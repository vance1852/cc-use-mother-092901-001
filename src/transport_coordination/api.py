"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .admission_service import AdmissionService
from .errors import DomainError, ValidationError
from .service import DomainService
from .storage import Database


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")
    admission = AdmissionService(service.database, service.clock)

    def parameter(name: str, default: str | None = None) -> str | None:
        return query.get(name, [default])[0]

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
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}

        # ------------------------------------------------------- 准入退出域写入
        if method == "POST" and parsed.path == "/policies":
            receipt = admission.publish_policy(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/investors":
            receipt = admission.register_investor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/investor-relations":
            receipt = admission.link_investor_relation(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/projects":
            receipt = admission.publish_project_version(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/evidence":
            receipt = admission.record_evidence(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/evidence-verifications":
            receipt = admission.verify_evidence(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/guarantees":
            receipt = admission.register_guarantee(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/capital-plans":
            receipt = admission.submit_capital_plan(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/obligations":
            receipt = admission.record_obligation(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/obligation-updates":
            receipt = admission.update_obligation(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/reviews":
            receipt = admission.propose_review(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/review-approvals":
            receipt = admission.record_approval(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__

        # ------------------------------------------------------- 准入退出域查询
        if method == "GET" and parsed.path == "/policies":
            policy_id = parameter("policy_id", "")
            if not policy_id:
                raise ValidationError("policy_id 不能为空")
            version_raw = parameter("version")
            version = int(version_raw) if version_raw else None
            return 200, asdict(admission.get_policy(policy_id, version))
        if method == "GET" and parsed.path == "/investor-groups":
            investor_id = parameter("investor_id", "")
            if not investor_id:
                raise ValidationError("investor_id 不能为空")
            return 200, admission.investor_group(investor_id)
        if method == "GET" and parsed.path == "/projects":
            project_id = parameter("project_id", "")
            if not project_id:
                raise ValidationError("project_id 不能为空")
            return 200, asdict(admission.get_project(project_id, parameter("as_of")))
        if method == "GET" and parsed.path == "/guarantees":
            guarantee_id = parameter("guarantee_id", "")
            if not guarantee_id:
                raise ValidationError("guarantee_id 不能为空")
            return 200, asdict(admission.get_guarantee(guarantee_id))
        if method == "GET" and parsed.path == "/reviews":
            review_id = parameter("review_id", "")
            if not review_id:
                raise ValidationError("review_id 不能为空")
            return 200, asdict(admission.get_review(review_id, parameter("as_of")))
        if method == "GET" and parsed.path == "/review-queue":
            return 200, {"items": admission.pending_queue()}
        if method == "GET" and parsed.path == "/project-snapshot":
            project_id = parameter("project_id", "")
            if not project_id:
                raise ValidationError("project_id 不能为空")
            return 200, admission.snapshot(project_id, parameter("as_of"))
        if method == "GET" and parsed.path == "/scenarios":
            project_id = parameter("project_id", "")
            if not project_id:
                raise ValidationError("project_id 不能为空")
            stage_label = parameter("stage_label", "main") or "main"
            return 200, admission.compare_scenarios(project_id, stage_label)
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


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
