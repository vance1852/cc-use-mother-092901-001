"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .participation import ParticipationService
from .service import DomainService
from .storage import Database


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    participation = ParticipationService(service.database, service.clock)
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}

        status, payload = _route_participation(participation, method, parsed, body, actor_id)
        if status is not None:
            return status, payload
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _route_participation(participation: ParticipationService, method: str, parsed,
                         body: dict[str, Any], actor_id: str) -> tuple[int | None, dict[str, Any]]:
    """分派社会资本准入退出相关路由。"""

    path = parsed.path
    query = parse_qs(parsed.query)
    p = participation

    def created(result: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return (200 if result.get("replayed") else 201), result

    if method == "POST":
        if path == "/rule-versions":
            return created(p.publish_rule_version(actor_id=actor_id, **body))
        if path == "/investors":
            return created(p.register_investor(actor_id=actor_id, **body))
        if path == "/investor-relations":
            return created(p.register_relation(actor_id=actor_id, **body))
        if path == "/assets":
            return created(p.register_asset(actor_id=actor_id, **body))
        if path == "/evidence":
            return created(p.register_evidence(actor_id=actor_id, **body))
        if path == "/projects":
            return created(p.create_project(actor_id=actor_id, **body))
        if path == "/review-tasks":
            return created(p.submit_review_task(actor_id=actor_id, **body))

        match = re.fullmatch(r"/investor-relations/([^/]+)/revoke", path)
        if match:
            return created(p.revoke_relation(actor_id=actor_id,
                                             relation_id=match.group(1), **body))
        match = re.fullmatch(r"/evidence/([^/]+)/(verify|revoke)", path)
        if match:
            evidence_id, action = match.groups()
            func = p.verify_evidence if action == "verify" else p.revoke_evidence
            return created(func(actor_id=actor_id, evidence_id=evidence_id, **body))
        match = re.fullmatch(r"/projects/([^/]+)/versions", path)
        if match:
            return created(p.publish_project_version(actor_id=actor_id,
                                                     project_id=match.group(1), **body))
        match = re.fullmatch(r"/projects/([^/]+)/applications", path)
        if match:
            return created(p.submit_application(actor_id=actor_id,
                                                project_id=match.group(1), **body))
        match = re.fullmatch(r"/projects/([^/]+)/compare-candidates", path)
        if match:
            return 200, p.compare_candidates(project_id=match.group(1), **body)
        match = re.fullmatch(r"/applications/([^/]+)/guarantees", path)
        if match:
            return created(p.post_guarantee(actor_id=actor_id,
                                            application_id=match.group(1), **body))
        match = re.fullmatch(r"/review-tasks/([^/]+)/sign", path)
        if match:
            return created(p.sign_review_task(actor_id=actor_id,
                                              task_id=match.group(1), **body))
        match = re.fullmatch(r"/agreements/([^/]+)/obligations", path)
        if match:
            return created(p.register_obligation(actor_id=actor_id,
                                                 agreement_id=match.group(1), **body))
        match = re.fullmatch(r"/obligations/([^/]+)/fulfill", path)
        if match:
            return created(p.fulfill_obligation(actor_id=actor_id,
                                                obligation_id=match.group(1), **body))

    if method == "GET":
        if path == "/review-tasks":
            status_filter = query.get("status", ["pending"])[0]
            return 200, {"items": p.list_review_queue(status_filter)}
        match = re.fullmatch(r"/applications/([^/]+)", path)
        if match:
            return 200, p.get_application(match.group(1))
        match = re.fullmatch(r"/agreements/([^/]+)", path)
        if match:
            return 200, p.get_agreement(match.group(1))
        match = re.fullmatch(r"/projects/([^/]+)/snapshot", path)
        if match:
            at = query.get("at", [None])[0]
            return 200, p.project_snapshot(match.group(1), at)
    return None, {}



class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DomainService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
