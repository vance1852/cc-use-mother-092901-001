"""交通项目社会资本准入退出领域服务。

职责：
- 项目版本、规则版本、证据、出资计划、履约担保的统一登记；
- 准入、分期入场、成员增减、控制权变更、违约退出五类会签审查；
- 投资人关联关系互斥、竞争申请唯一生效、资产防重复承诺；
- 退出时结清未完成义务、追索金额与可退担保；
- 任一时点的规则、证据、批准链还原与候选方案比较。

所有写入仅追加 admission_events，并在基础哈希审计链上留痕，
因此规则换版不能倒改已签协议，重启后待审队列顺序不变。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict
from typing import Any

from .admission_models import (
    AdmissionReceipt, Approval, ExitSettlement, Guarantee, Investor, PolicyVersion,
    Project, ProjectVersion, ReviewTask,
)
from .admission_store import AdmissionStore
from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .projection import Projection, build_projection
from .storage import Database

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

TASK_TYPES = frozenset({"qualification", "entry", "member_change", "control_change", "exit"})
STRUCTURAL_TYPES = frozenset({"member_change", "control_change", "exit"})
OWNERSHIP_KINDS = frozenset({"private", "state_owned", "collective"})
GUARANTEE_TYPES = frozenset({"bid_bond", "performance_bond", "capital_guarantee"})
DEFAULT_STAGE = "main"


class AdmissionService:
    """准入退出域的应用服务，一个实例对应一个数据库。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        self.store = AdmissionStore(database)

    # ------------------------------------------------------------------ 基础工具

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _id(self, value: Any, field: str) -> str:
        value = str(value or "").strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: Any, field: str, limit: int = 200) -> str:
        value = str(value or "").strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _number(self, value: Any, field: str, *, minimum: float = 0.0) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"{field} 必须是数字")
        value = float(value)
        if value < minimum:
            raise ValidationError(f"{field} 不能小于 {minimum}")
        return value

    def _ratio(self, value: Any, field: str) -> float:
        value = self._number(value, field)
        if value > 1:
            raise ValidationError(f"{field} 必须是 0 到 1 之间的比例")
        return value

    def _actor(self, connection, actor_id: str):
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require(self, actor, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _projection(self, connection, as_of: str | None = None) -> Projection:
        return build_projection(self.store.stream(connection, as_of=as_of))

    def _emit(self, connection, *, aggregate_type: str, aggregate_id: str, event_type: str,
              payload: dict[str, Any], actor_id: str, request_id: str | None) -> dict[str, Any]:
        event = self.store.append(
            connection, aggregate_type=aggregate_type, aggregate_id=aggregate_id,
            event_type=event_type, payload=payload, actor_id=actor_id,
            request_id=request_id, created_at=self._now())
        append_event(
            connection, actor_id=actor_id, action=f"admission.{event_type}",
            resource_type=aggregate_type, resource_id=aggregate_id,
            detail={"event_id": event["event_id"], "sequence": event["sequence"],
                    "payload_hash": digest(payload)}, occurred_at=self._now())
        return event

    def _write(self, *, request_id: str, actor_id: str, action: str, payload: dict[str, Any],
               allowed_roles: tuple[str, ...],
               work) -> AdmissionReceipt:
        request_id = self._id(request_id, "request_id")
        payload_hash = digest(payload)
        replayed = self.store.get_receipt(request_id)
        if replayed is not None:
            if replayed["action"] != action or replayed["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            response = replayed["response"]
            return AdmissionReceipt(request_id, action, response["resource_type"],
                                    response["resource_id"], True)
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *allowed_roles)
            locked = connection.execute(
                "SELECT 1 FROM admission_receipts WHERE request_id=?", (request_id,)).fetchone()
            if locked is not None:
                raise ConflictError("request_id 正在被并发使用")
            resource_type, resource_id, response = work(connection, actor)
            response = {"resource_type": resource_type, "resource_id": resource_id, **response}
            self.store.save_receipt(connection, request_id=request_id, action=action,
                                    payload_hash=payload_hash, response=response,
                                    created_at=self._now())
        return AdmissionReceipt(request_id, action, response["resource_type"],
                                response["resource_id"], False)

    # ------------------------------------------------------------------ 规则版本

    def publish_policy(self, *, request_id: str, actor_id: str, policy_id: str,
                       thresholds: dict[str, Any], note: str = "") -> AdmissionReceipt:
        policy_id = self._id(policy_id, "policy_id")
        if not isinstance(thresholds, dict) or not thresholds:
            raise ValidationError("thresholds 必须是非空对象")
        clean = self._clean_thresholds(thresholds)
        payload = {"actor_id": actor_id, "policy_id": policy_id,
                   "thresholds": clean, "note": note}

        def work(connection, actor) -> tuple[str, str, dict[str, Any]]:
            projection = self._projection(connection)
            existing = projection.policies.get(policy_id)
            version = (existing["active_version"] + 1) if existing else 1
            event_payload = {"policy_id": policy_id, "version": version,
                             "thresholds": clean, "published_by": actor["actor_id"],
                             "published_at": self._now(), "effective_at": self._now(),
                             "status": "active", "note": note}
            self._emit(connection, aggregate_type="policy", aggregate_id=policy_id,
                       event_type="policy.published", payload=event_payload,
                       actor_id=actor["actor_id"], request_id=request_id)
            return "policy", policy_id, {"version": version, "thresholds_hash": digest(clean)}

        return self._write(request_id=request_id, actor_id=actor_id,
                           action="publish_policy", payload=payload,
                           allowed_roles=("admin",), work=work)

    def _clean_thresholds(self, thresholds: dict[str, Any]) -> dict[str, Any]:
        known = {
            "min_private_ratio": self._ratio,
            "min_guarantee_ratio": self._ratio,
            "max_controller_share": self._ratio,
            "breach_forfeit_ratio": self._ratio,
        }
        clean: dict[str, Any] = {}
        for key, value in thresholds.items():
            if key in known:
                clean[key] = known[key](value, key)
            elif key in {"required_evidence_types", "recommended_evidence_types"}:
                if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
                    raise ValidationError(f"{key} 必须是字符串数组")
                clean[key] = value
            elif key == "require_verified_evidence":
                clean[key] = bool(value)
            else:
                raise ValidationError(f"未知门槛字段: {key}")
        clean.setdefault("min_private_ratio", 0.0)
        clean.setdefault("min_guarantee_ratio", 0.0)
        clean.setdefault("required_evidence_types", [])
        clean.setdefault("recommended_evidence_types", [])
        clean.setdefault("require_verified_evidence", True)
        clean.setdefault("max_controller_share", 1.0)
        clean.setdefault("breach_forfeit_ratio", 1.0)
        return clean

    def get_policy(self, policy_id: str, version: int | None = None) -> PolicyVersion:
        projection = build_projection(self.store.read_events(aggregate_type="policy",
                                                             aggregate_id=policy_id))
        policy = projection.policies.get(policy_id)
        if policy is None:
            raise NotFoundError("规则不存在")
        version = version or policy["active_version"]
        payload = policy["versions"].get(version)
        if payload is None:
            raise NotFoundError("规则版本不存在")
        return PolicyVersion(payload["policy_id"], payload["version"], payload["thresholds"],
                             payload["published_by"], payload["published_at"],
                             payload["effective_at"], payload["status"])

    def _active_policy(self, projection: Projection, policy_id: str | None) -> dict[str, Any]:
        if not projection.policies:
            raise ValidationError("尚未发布任何准入规则")
        if policy_id is None:
            policy_id = sorted(projection.policies)[0]
        policy = projection.policies.get(policy_id)
        if policy is None:
            raise NotFoundError("规则不存在")
        return policy["versions"][policy["active_version"]]

    # ------------------------------------------------------------------ 投资人与关联

    def register_investor(self, *, request_id: str, actor_id: str, investor_id: str,
                          name: str, ownership_kind: str) -> AdmissionReceipt:
        investor_id = self._id(investor_id, "investor_id")
        name = self._text(name, "name")
        if ownership_kind not in OWNERSHIP_KINDS:
            raise ValidationError("ownership_kind 不在允许范围内")
        payload = {"actor_id": actor_id, "investor_id": investor_id,
                   "name": name, "ownership_kind": ownership_kind}

        def work(connection, actor) -> tuple[str, str, dict[str, Any]]:
            projection = self._projection(connection)
            if investor_id in projection.investors:
                raise ConflictError("投资人编号已经存在")
            event_payload = {"investor_id": investor_id, "name": name,
                             "ownership_kind": ownership_kind, "created_at": self._now()}
            self._emit(connection, aggregate_type="investor", aggregate_id=investor_id,
                       event_type="investor.registered", payload=event_payload,
                       actor_id=actor["actor_id"], request_id=request_id)
            return "investor", investor_id, {"investor_id": investor_id}

        return self._write(request_id=request_id, actor_id=actor_id,
                           action="register_investor", payload=payload,
                           allowed_roles=("admin", "operator"), work=work)

    def link_investor_relation(self, *, request_id: str, actor_id: str,
                               left_id: str, right_id: str,
                               relation_type: str) -> AdmissionReceipt:
        left_id = self._id(left_id, "left_id")
        right_id = self._id(right_id, "right_id")
        relation_type = self._text(relation_type, "relation_type", 80)
        if left_id == right_id:
            raise ValidationError("关联双方不能相同")
        payload = {"actor_id": actor_id, "left_id": left_id, "right_id": right_id,
                   "relation_type": relation_type}

        def work(connection, actor) -> tuple[str, str, dict[str, Any]]:
            projection = self._projection(connection)
            if left_id not in projection.investors or right_id not in projection.investors:
                raise NotFoundError("投资人不存在")
            event_payload = {"left_id": left_id, "right_id": right_id,
                             "relation_type": relation_type}
            self._emit(connection, aggregate_type="investor_relation",
                       aggregate_id=f"{left_id}|{right_id}",
                       event_type="investor.relation.linked", payload=event_payload,
                       actor_id=actor["actor_id"], request_id=request_id)
            group = projection.union_find.group(left_id) | projection.union_find.group(right_id)
            return "investor_relation", f"{left_id}|{right_id}", {"group_size": len(group)}

        return self._write(request_id=request_id, actor_id=actor_id,
                           action="link_investor_relation", payload=payload,
                           allowed_roles=("admin", "operator"), work=work)

    def investor_group(self, investor_id: str) -> dict[str, Any]:
        projection = self._projection(self.database.connection)
        if investor_id not in projection.investors:
            raise NotFoundError("投资人不存在")
        members = sorted(projection.related_group(investor_id))
        links = [{"left_id": item["left_id"], "right_id": item["right_id"],
                  "relation_type": item["relation_type"]}
                 for item in projection.relations
                 if item["left_id"] in members or item["right_id"] in members]
        return {"investor_id": investor_id, "members": members, "links": links}

    # ------------------------------------------------------------------ 项目版本

    def publish_project_version(self, *, request_id: str, actor_id: str, project_id: str,
                                name: str, total_investment: float,
                                private_target_ratio: float,
                                assets: list[str]) -> AdmissionReceipt:
        project_id = self._id(project_id, "project_id")
        name = self._text(name, "name")
        total_investment = self._number(total_investment, "total_investment", minimum=0.01)
        private_target_ratio = self._ratio(private_target_ratio, "private_target_ratio")
        if not isinstance(assets, list) or not assets:
            raise ValidationError("assets 必须是非空数组")
        assets = [self._id(item, "assets[]") for item in assets]
        if len(set(assets)) != len(assets):
            raise ValidationError("资产清单不能重复")
        payload = {"actor_id": actor_id, "project_id": project_id, "name": name,
                   "total_investment": total_investment,
                   "private_target_ratio": private_target_ratio, "assets": assets}

        def work(connection, actor) -> tuple[str, str, dict[str, Any]]:
            projection = self._projection(connection)
            existing = projection.projects.get(project_id)
            version = (existing["current_version"] + 1) if existing else 1
            event_payload = {"project_id": project_id, "version": version, "name": name,
                             "total_investment": total_investment,
                             "private_target_ratio": private_target_ratio,
                             "assets": assets, "published_at": self._now()}
            self._emit(connection, aggregate_type="project", aggregate_id=project_id,
                       event_type="project.published", payload=event_payload,
                       actor_id=actor["actor_id"], request_id=request_id)
            return "project", project_id, {"version": version}

        return self._write(request_id=request_id, actor_id=actor_id,
                           action="publish_project_version", payload=payload,
                           allowed_roles=("admin",), work=work)

    def get_project(self, project_id: str, as_of: str | None = None) -> Project:
        events = self.store.read_events(aggregate_type="project", aggregate_id=project_id,
                                        as_of=as_of)
        projection = build_projection(events)
        project = projection.projects.get(project_id)
        if project is None:
            raise NotFoundError("项目不存在")
        versions = tuple(
            ProjectVersion(payload["project_id"], payload["version"], payload["name"],
                           payload["total_investment"], payload["private_target_ratio"],
                           tuple(payload["assets"]), payload["published_at"])
            for payload in sorted(project["versions"].values(), key=lambda item: item["version"]))
        return Project(project_id, project["current_version"], versions)

    # ------------------------------------------------------------------ 证据与担保

    def record_evidence(self, *, request_id: str, actor_id: str, evidence_id: str,
                        investor_id: str, evidence_type: str, document_ref: str,
                        content_hash: str, valid_from: str,
                        valid_until: str) -> AdmissionReceipt:
        evidence_id = self._id(evidence_id, "evidence_id")
        investor_id = self._id(investor_id, "investor_id")
        evidence_type = self._text(evidence_type, "evidence_type", 80)
        document_ref = self._text(document_ref, "document_ref", 300)
        content_hash = self._text(content_hash, "content_hash", 128)
        valid_from = self._text(valid_from, "valid_from", 40)
        valid_until = self._text(valid_until, "valid_until", 40)
        payload = {"actor_id": actor_id, "evidence_id": evidence_id,
                   "investor_id": investor_id, "evidence_type": evidence_type,
                   "document_ref": document_ref, "content_hash": content_hash,
                   "valid_from": valid_from, "valid_until": valid_until}

        def work(connection, actor) -> tuple[str, str, dict[str, Any]]:
            projection = self._projection(connection)
            if investor_id not in projection.investors:
                raise NotFoundError("投资人不存在")
            if evidence_id in projection.evidence:
                raise ConflictError("证据编号已经存在")
            event_payload = {"evidence_id": evidence_id, "investor_id": investor_id,
                             "evidence_type": evidence_type, "document_ref": document_ref,
                             "content_hash": content_hash, "valid_from": valid_from,
                             "valid_until": valid_until}
            self._emit(connection, aggregate_type="evidence", aggregate_id=evidence_id,
                       event_type="evidence.recorded", payload=event_payload,
                       actor_id=actor["actor_id"], request_id=request_id)
            return "evidence", evidence_id, {"content_hash": content_hash}

        return self._write(request_id=request_id, actor_id=actor_id,
                           action="record_evidence", payload=payload,
                           allowed_roles=("admin", "operator"), work=work)

    def verify_evidence(self, *, request_id: str, actor_id: str, evidence_id: str,
                        verified: bool, comment: str = "") -> AdmissionReceipt:
        evidence_id = self._id(evidence_id, "evidence_id")
        comment = str(comment or "")[:300]
        payload = {"actor_id": actor_id, "evidence_id": evidence_id,
                   "verified": bool(verified), "comment": comment}

        def work(connection, actor) -> tuple[str, str, dict[str, Any]]:
            projection = self._projection(connection)
            if evidence_id not in projection.evidence:
                raise NotFoundError("证据不存在")
            event_payload = {"evidence_id": evidence_id, "verified": bool(verified),
                             "verified_by": actor["actor_id"], "comment": comment}
            self._emit(connection, aggregate_type="evidence", aggregate_id=evidence_id,
                       event_type="evidence.verified", payload=event_payload,
                       actor_id=actor["actor_id"], request_id=request_id)
            return "evidence", evidence_id, {"verified": bool(verified)}

        return self._write(request_id=request_id, actor_id=actor_id,
                           action="verify_evidence", payload=payload,
                           allowed_roles=("reviewer",), work=work)

    def register_guarantee(self, *, request_id: str, actor_id: str, guarantee_id: str,
                           investor_id: str, guarantee_type: str, amount: float,
                           currency: str = "CNY", reference: str = "") -> AdmissionReceipt:
        guarantee_id = self._id(guarantee_id, "guarantee_id")
        investor_id = self._id(investor_id, "investor_id")
        if guarantee_type not in GUARANTEE_TYPES:
            raise ValidationError("guarantee_type 不在允许范围内")
        amount = self._number(amount, "amount", minimum=0.01)
        currency = self._text(currency, "currency", 8)
        reference = str(reference or "")[:300]
        payload = {"actor_id": actor_id, "guarantee_id": guarantee_id,
                   "investor_id": investor_id, "guarantee_type": guarantee_type,
                   "amount": amount, "currency": currency, "reference": reference}

        def work(connection, actor) -> tuple[str, str, dict[str, Any]]:
            projection = self._projection(connection)
            if investor_id not in projection.investors:
                raise NotFoundError("投资人不存在")
            if guarantee_id in projection.guarantees:
                raise ConflictError("担保编号已经存在")
            event_payload = {"guarantee_id": guarantee_id, "investor_id": investor_id,
                             "guarantee_type": guarantee_type, "amount": amount,
                             "currency": currency, "reference": reference}
            self._emit(connection, aggregate_type="guarantee", aggregate_id=guarantee_id,
                       event_type="guarantee.registered", payload=event_payload,
                       actor_id=actor["actor_id"], request_id=request_id)
            return "guarantee", guarantee_id, {"amount": amount}

        return self._write(request_id=request_id, actor_id=actor_id,
                           action="register_guarantee", payload=payload,
                           allowed_roles=("admin", "operator"), work=work)

    def get_guarantee(self, guarantee_id: str) -> Guarantee:
        projection = self._projection(self.database.connection)
        item = projection.guarantees.get(guarantee_id)
        if item is None:
            raise NotFoundError("担保不存在")
        return Guarantee(item["guarantee_id"], item["investor_id"], item["guarantee_type"],
                         item["amount"], item["currency"], item["status"],
                         item.get("reference", ""), tuple(item["history"]))

    # ------------------------------------------------------------------ 出资计划

    def submit_capital_plan(self, *, request_id: str, actor_id: str, plan_id: str,
                            project_id: str, investor_id: str, total_amount: float,
                            private_amount: float,
                            tranches: list[dict[str, Any]]) -> AdmissionReceipt:
        plan_id = self._id(plan_id, "plan_id")
        project_id = self._id(project_id, "project_id")
        investor_id = self._id(investor_id, "investor_id")
        total_amount = self._number(total_amount, "total_amount", minimum=0.01)
        private_amount = self._number(private_amount, "private_amount")
        if private_amount > total_amount:
            raise ValidationError("民营出资额不能超过出资总额")
        clean_tranches = self._clean_tranches(tranches, total_amount)
        payload_hash_src = {"actor_id": actor_id, "plan_id": plan_id,
                            "project_id": project_id, "investor_id": investor_id,
                            "total_amount": total_amount, "private_amount": private_amount,
                            "tranches": clean_tranches}

        def work(connection, actor) -> tuple[str, str, dict[str, Any]]:
            projection = self._projection(connection)
            if project_id not in projection.projects:
                raise NotFoundError("项目不存在")
            if investor_id not in projection.investors:
                raise NotFoundError("投资人不存在")
            if plan_id in projection.capital_plans:
                raise ConflictError("出资计划编号已经存在")
            ratio = round(private_amount / total_amount, 6)
            event_payload = {"plan_id": plan_id, "project_id": project_id,
                             "investor_id": investor_id, "total_amount": total_amount,
                             "private_amount": private_amount, "private_ratio": ratio,
                             "tranches": clean_tranches, "submitted_at": self._now()}
            self._emit(connection, aggregate_type="capital_plan", aggregate_id=plan_id,
                       event_type="capital_plan.submitted", payload=event_payload,
                       actor_id=actor["actor_id"], request_id=request_id)
            return "capital_plan", plan_id, {"private_ratio": ratio,
                                             "payload_hash": digest(event_payload)}

        return self._write(request_id=request_id, actor_id=actor_id,
                           action="submit_capital_plan", payload=payload_hash_src,
                           allowed_roles=("admin", "operator"), work=work)

    def _clean_tranches(self, tranches: list[dict[str, Any]],
                        total_amount: float) -> list[dict[str, Any]]:
        if not isinstance(tranches, list) or not tranches:
            raise ValidationError("tranches 必须是非空数组")
        clean = []
        total = 0.0
        for index, item in enumerate(tranches):
            if not isinstance(item, dict):
                raise ValidationError(f"第 {index + 1} 期出资必须是对象")
            label = self._text(item.get("label"), f"tranches[{index}].label", 80)
            amount = self._number(item.get("amount"), f"tranches[{index}].amount", minimum=0.01)
            scheduled_at = self._text(item.get("scheduled_at"),
                                      f"tranches[{index}].scheduled_at", 40)
            clean.append({"label": label, "amount": amount, "scheduled_at": scheduled_at})
            total += amount
        if round(total, 2) != round(total_amount, 2):
            raise ValidationError("各期出资之和必须等于出资总额")
        return clean

    # ------------------------------------------------------------------ 义务登记

    def record_obligation(self, *, request_id: str, actor_id: str, obligation_id: str,
                          project_id: str, investor_id: str, obligation_type: str,
                          description: str, amount: float, due_at: str,
                          status: str = "pending") -> AdmissionReceipt:
        obligation_id = self._id(obligation_id, "obligation_id")
        project_id = self._id(project_id, "project_id")
        investor_id = self._id(investor_id, "investor_id")
        obligation_type = self._text(obligation_type, "obligation_type", 80)
        description = self._text(description, "description", 300)
        amount = self._number(amount, "amount")
        due_at = self._text(due_at, "due_at", 40)
        if status not in {"pending", "in_progress", "paid", "completed"}:
            raise ValidationError("义务状态不在允许范围内")

        def work(connection, actor) -> tuple[str, str, dict[str, Any]]:
            projection = self._projection(connection)
            if project_id not in projection.projects:
                raise NotFoundError("项目不存在")
            if investor_id not in projection.investors:
                raise NotFoundError("投资人不存在")
            if obligation_id in projection.obligations:
                raise ConflictError("义务编号已经存在")
            event_payload = {"obligation_id": obligation_id, "project_id": project_id,
                             "investor_id": investor_id, "obligation_type": obligation_type,
                             "description": description, "amount": amount, "due_at": due_at,
                             "status": status}
            self._emit(connection, aggregate_type="obligation", aggregate_id=obligation_id,
                       event_type="obligation.recorded", payload=event_payload,
                       actor_id=actor["actor_id"], request_id=request_id)
            return "obligation", obligation_id, {"status": status}

        return self._write(request_id=request_id, actor_id=actor_id,
                           action="record_obligation", payload={
                               "actor_id": actor_id, "obligation_id": obligation_id,
                               "project_id": project_id, "investor_id": investor_id,
                               "obligation_type": obligation_type, "description": description,
                               "amount": amount, "due_at": due_at, "status": status},
                           allowed_roles=("admin", "operator"), work=work)

    def update_obligation(self, *, request_id: str, actor_id: str,
                          obligation_id: str, status: str) -> AdmissionReceipt:
        obligation_id = self._id(obligation_id, "obligation_id")
        if status not in {"pending", "in_progress", "paid", "completed"}:
            raise ValidationError("义务状态不在允许范围内")

        def work(connection, actor) -> tuple[str, str, dict[str, Any]]:
            projection = self._projection(connection)
            if obligation_id not in projection.obligations:
                raise NotFoundError("义务不存在")
            self._emit(connection, aggregate_type="obligation", aggregate_id=obligation_id,
                       event_type="obligation.updated",
                       payload={"obligation_id": obligation_id, "status": status},
                       actor_id=actor["actor_id"], request_id=request_id)
            return "obligation", obligation_id, {"status": status}

        return self._write(request_id=request_id, actor_id=actor_id,
                           action="update_obligation",
                           payload={"actor_id": actor_id, "obligation_id": obligation_id,
                                    "status": status},
                           allowed_roles=("admin", "operator"), work=work)

    # ------------------------------------------------------------------ 会签审查

    def propose_review(self, *, request_id: str, actor_id: str, review_id: str,
                       project_id: str, task_type: str, applicant_id: str,
                       proposal: dict[str, Any], policy_id: str | None = None
                       ) -> AdmissionReceipt:
        review_id = self._id(review_id, "review_id")
        project_id = self._id(project_id, "project_id")
        applicant_id = self._id(applicant_id, "applicant_id")
        if task_type not in TASK_TYPES:
            raise ValidationError("task_type 不在允许范围内")
        if not isinstance(proposal, dict) or not proposal:
            raise ValidationError("proposal 必须是非空对象")
        stage_label = str(proposal.get("stage_label") or DEFAULT_STAGE).strip() or DEFAULT_STAGE
        stage_label = self._id(stage_label, "stage_label")

        def work(connection, actor) -> tuple[str, str, dict[str, Any]]:
            projection = self._projection(connection)
            if project_id not in projection.projects:
                raise NotFoundError("项目不存在")
            if applicant_id not in projection.investors:
                raise NotFoundError("申请人(投资人)不存在")
            if review_id in projection.reviews:
                raise ConflictError("审查编号已经存在")
            project = projection.projects[project_id]
            project_payload = project["versions"][project["current_version"]]
            if task_type in STRUCTURAL_TYPES:
                # 结构性变更作用于已签协议，沿用协议冻结的规则版本，新版规则不倒改旧协议。
                qualification = self._latest_awarded(projection, project_id, "qualification")
                if qualification is None:
                    raise ValidationError("项目尚无生效协议，不能发起该类审查")
                pin = qualification["policy_version"]
                policy_payload = (projection.policies[pin["policy_id"]]["versions"]
                                  [pin["version"]])
            else:
                policy_payload = self._active_policy(projection, policy_id)

            pending_for_project = [r for r in projection.pending_reviews()
                                   if r["project_id"] == project_id]
            if task_type in STRUCTURAL_TYPES and pending_for_project:
                raise ConflictError("项目存在尚未处理完毕的审查，应按队列顺序办理")
            if task_type in {"qualification", "entry"} and any(
                    r["task_type"] in STRUCTURAL_TYPES for r in pending_for_project):
                raise ConflictError("项目有成员、控制权或退出审查未决，暂不受理新申请")

            clean_proposal: dict[str, Any] = {"stage_label": stage_label}
            evidence_ids: list[str] = []
            guarantee_ids: list[str] = []
            assets: list[str] = []

            if task_type == "qualification":
                clean_proposal.update(self._prepare_qualification(
                    projection, project_id, stage_label, applicant_id, proposal, policy_payload))
                evidence_ids = clean_proposal["evidence_ids"]
                guarantee_ids = clean_proposal["guarantee_ids"]
                assets = clean_proposal["assets"]
            elif task_type == "entry":
                clean_proposal.update(self._prepare_entry(
                    projection, project_id, stage_label, applicant_id, proposal))
                assets = clean_proposal["assets"]
                guarantee_ids = clean_proposal.get("guarantee_ids", [])
                evidence_ids = clean_proposal.get("evidence_ids", [])
            elif task_type == "member_change":
                clean_proposal.update(self._prepare_member_change(
                    projection, project_id, stage_label, applicant_id, proposal))
                assets = clean_proposal.get("assets", [])
            elif task_type == "control_change":
                clean_proposal.update(self._prepare_control_change(
                    projection, project_id, stage_label, applicant_id, proposal))
            else:
                clean_proposal.update(self._prepare_exit(
                    projection, project_id, stage_label, applicant_id, proposal))

            self._occupy_assets(connection, projection, project_id=project_id,
                                review_id=review_id, investor_id=applicant_id,
                                assets=assets, request_id=request_id,
                                actor_id=actor["actor_id"], reason="审查申请预留")

            competing = self._competing_pending(projection, project_id, stage_label,
                                                task_type, review_id)
            evidence_hashes = self._evidence_hashes(projection, evidence_ids)
            event_payload = {
                "review_id": review_id, "project_id": project_id, "task_type": task_type,
                "applicant_id": applicant_id, "status": "pending", "outcome": "",
                "message": "", "project_version": project_payload["version"],
                "policy_version": {"policy_id": policy_payload["policy_id"],
                                   "version": policy_payload["version"],
                                   "thresholds_hash": digest(policy_payload["thresholds"])},
                "proposal": clean_proposal, "evidence_hashes": evidence_hashes,
                "approvals": [], "competing": competing,
                "sealed_agreement": None, "decided_at": None,
                "created_by": actor["actor_id"], "created_at": self._now()}
            self._emit(connection, aggregate_type="review", aggregate_id=review_id,
                       event_type="review.proposed", payload=event_payload,
                       actor_id=actor["actor_id"], request_id=request_id)
            return "review", review_id, {"status": "pending", "position": len(projection.pending_reviews()) + 1}

        return self._write(request_id=request_id, actor_id=actor_id,
                           action="propose_review",
                           payload={"actor_id": actor_id, "review_id": review_id,
                                    "project_id": project_id, "task_type": task_type,
                                    "applicant_id": applicant_id, "proposal": proposal,
                                    "policy_id": policy_id},
                           allowed_roles=("admin", "operator"), work=work)

    def _prepare_qualification(self, projection: Projection, project_id: str,
                               stage_label: str, applicant_id: str,
                               proposal: dict[str, Any], policy_payload: dict[str, Any]
                               ) -> dict[str, Any]:
        consortium = self._clean_consortium(proposal.get("consortium"), projection,
                                            require_leader=applicant_id)
        evidence_ids = self._clean_evidence(proposal.get("evidence_ids"), projection)
        guarantee_ids = self._clean_guarantees(proposal.get("guarantee_ids"), projection)
        assets = [self._id(item, "assets[]") for item in proposal.get("assets", [])]
        if len(set(assets)) != len(assets):
            raise ValidationError("承诺资产不能重复")
        plan_id = proposal.get("capital_plan_id")
        plan = projection.capital_plans.get(self._id(plan_id, "capital_plan_id")) \
            if plan_id else None
        if plan is None or plan["project_id"] != project_id:
            raise NotFoundError("出资计划不存在或不属于该项目")

        thresholds = policy_payload["thresholds"]
        if plan["private_ratio"] + 1e-9 < thresholds["min_private_ratio"]:
            raise ValidationError("民营出资比例低于规则门槛")
        coverage = sum(projection.guarantees[gid]["amount"] for gid in guarantee_ids)
        if coverage + 1e-9 < plan["total_amount"] * thresholds["min_guarantee_ratio"]:
            raise ValidationError("履约担保覆盖比例低于规则门槛")
        required = set(thresholds["required_evidence_types"])
        present = {projection.evidence[eid]["evidence_type"] for eid in evidence_ids}
        missing = required - present
        if missing:
            raise ValidationError(f"缺少必备证据类型: {sorted(missing)}")
        if thresholds["require_verified_evidence"]:
            unverified = [eid for eid in evidence_ids
                          if not projection.evidence[eid]["verified"]]
            if unverified:
                raise ValidationError(f"证据尚未核验: {unverified}")
        # 关联关系互斥：同一项目同一阶段，关联组只能有一个在途/生效申请
        group = projection.related_group(applicant_id)
        for other in self._active_qualifications(projection, project_id, stage_label):
            others = set(other["proposal"]["consortium_members"])
            if group & others:
                raise ConflictError("关联投资主体已在本项目本阶段提交申请，禁止重复申报")
        controller = next((c["investor_id"] for c in consortium if c.get("controller")), None)
        checks = {"private_ratio": plan["private_ratio"],
                  "guarantee_coverage": round(coverage / plan["total_amount"], 6),
                  "thresholds_hash": digest(thresholds)}
        return {"consortium": consortium, "consortium_members": [c["investor_id"] for c in consortium],
                "controller": controller, "evidence_ids": evidence_ids,
                "guarantee_ids": guarantee_ids, "assets": assets,
                "capital_plan_id": plan["plan_id"], "threshold_checks": checks}

    def _prepare_entry(self, projection: Projection, project_id: str, stage_label: str,
                       applicant_id: str, proposal: dict[str, Any]) -> dict[str, Any]:
        awarded = projection.awarded_review(project_id, stage_label, "entry")
        if awarded is not None:
            raise ConflictError("该阶段已有生效的入场结果")
        qualification = self._latest_awarded(projection, project_id, "qualification")
        if qualification is None:
            raise ValidationError("项目尚无生效的资格准入结果，不能分期入场")
        members = set(qualification["proposal"]["consortium_members"])
        entrants = proposal.get("entrants")
        if not isinstance(entrants, list) or not entrants:
            raise ValidationError("entrants 必须是非空数组")
        entrants = [self._id(item, "investor_id") for item in entrants]
        unknown = [item for item in entrants if item not in members]
        if unknown:
            raise ValidationError(f"入场主体不在已准入联合体: {unknown}")
        tranche = proposal.get("tranche")
        if not isinstance(tranche, dict):
            raise ValidationError("tranche 必须是分期出资对象")
        tranche = {"label": self._text(tranche.get("label"), "tranche.label", 80),
                   "amount": self._number(tranche.get("amount"), "tranche.amount", minimum=0.01),
                   "scheduled_at": self._text(tranche.get("scheduled_at"),
                                              "tranche.scheduled_at", 40)}
        assets = [self._id(item, "assets[]") for item in proposal.get("assets", [])]
        if len(set(assets)) != len(assets):
            raise ValidationError("承诺资产不能重复")
        guarantee_ids = self._clean_guarantees(proposal.get("guarantee_ids", []), projection)
        evidence_ids = self._clean_evidence(proposal.get("evidence_ids", []), projection)
        return {"entrants": entrants, "tranche": tranche, "assets": assets,
                "guarantee_ids": guarantee_ids, "evidence_ids": evidence_ids}

    def _prepare_member_change(self, projection: Projection, project_id: str,
                               stage_label: str, applicant_id: str,
                               proposal: dict[str, Any]) -> dict[str, Any]:
        awarded = self._latest_awarded(projection, project_id, "qualification")
        if awarded is None:
            raise ValidationError("联合体尚不存在，不能变更成员")
        consortium = self._current_consortium(projection, project_id, stage_label)
        current = {item["investor_id"]: item for item in consortium}
        if applicant_id not in current:
            raise PermissionDenied("成员变更须由当前联合体成员发起")
        add_raw = proposal.get("add_members", [])
        remove = [self._id(item, "removed[]") for item in proposal.get("remove_members", [])]
        additions = []
        for index, item in enumerate(add_raw):
            if not isinstance(item, dict):
                raise ValidationError("add_members 必须是对象数组")
            investor_id = self._id(item.get("investor_id"), f"add_members[{index}].investor_id")
            if investor_id not in projection.investors:
                raise NotFoundError(f"新增成员不存在: {investor_id}")
            shares = self._ratio(item.get("shares"), f"add_members[{index}].shares")
            if investor_id in current:
                raise ConflictError(f"成员已在联合体中: {investor_id}")
            additions.append({"investor_id": investor_id, "shares": shares,
                              "controller": bool(item.get("controller"))})
        for investor_id in remove:
            if investor_id not in current:
                raise ValidationError(f"移除对象不在联合体中: {investor_id}")
            blocked = [o for o in projection.obligations.values()
                       if o["project_id"] == project_id and o["investor_id"] == investor_id
                       and o["status"] in {"pending", "in_progress"}]
            if blocked:
                raise ValidationError(f"{investor_id} 尚有未结清义务，应先走退出审查")
        overlap = {a["investor_id"] for a in additions} & set(remove)
        if overlap:
            raise ValidationError(f"同一主体不能同时加入和退出: {sorted(overlap)}")
        reason = self._text(proposal.get("reason", ""), "reason", 300)
        new_members = [dict(item) for item in consortium
                       if item["investor_id"] not in remove] + additions
        shares_sum = round(sum(item["shares"] for item in new_members), 6)
        if new_members and abs(shares_sum - 1) > 1e-6:
            raise ValidationError("变更后各方持股比例之和必须为 1")
        assets = [self._id(item, "assets[]") for item in proposal.get("assets", [])]
        return {"add_members": additions, "remove_members": remove, "reason": reason,
                "assets": assets, "resulting_members": new_members}

    def _prepare_control_change(self, projection: Projection, project_id: str,
                                stage_label: str, applicant_id: str,
                                proposal: dict[str, Any]) -> dict[str, Any]:
        consortium = self._current_consortium(projection, project_id, stage_label)
        if not consortium:
            raise ValidationError("联合体尚不存在，不能变更控制权")
        current_controller = self._current_controller(projection, project_id, stage_label)
        old_controller = self._id(proposal.get("old_controller", current_controller),
                                  "old_controller")
        if old_controller != current_controller:
            raise ValidationError("原控股方与当前生效结果不一致")
        new_controller = self._id(proposal.get("new_controller"), "new_controller")
        members = {item["investor_id"]: item for item in consortium}
        if new_controller not in members:
            raise ValidationError("新控股方必须是联合体成员")
        if applicant_id not in {old_controller, new_controller}:
            raise PermissionDenied("控制权变更须由原控股方或新控股方发起")
        if new_controller == old_controller:
            raise ValidationError("新控股方与原控股方相同")
        holdings = proposal.get("holdings")
        if not isinstance(holdings, dict):
            raise ValidationError("holdings 必须是变更后持股对象")
        clean_holdings = {self._id(k, "holdings.key"): self._ratio(v, "holdings.value")
                          for k, v in holdings.items()}
        if set(clean_holdings) != set(members):
            raise ValidationError("holdings 必须覆盖联合体全部成员")
        if abs(sum(clean_holdings.values()) - 1) > 1e-6:
            raise ValidationError("持股比例之和必须为 1")
        if abs(clean_holdings[new_controller] - members[new_controller]["shares"]) < 1e-9:
            raise ValidationError("控股方持股未发生变化")
        reason = self._text(proposal.get("reason", ""), "reason", 300)
        pinned_policy = self._policy_of_latest(projection, project_id)
        limit = pinned_policy["thresholds"]["max_controller_share"]
        if clean_holdings[new_controller] > limit + 1e-9:
            raise ValidationError("新控股方持股超过规则上限")
        return {"old_controller": old_controller, "new_controller": new_controller,
                "holdings": clean_holdings, "reason": reason}

    def _prepare_exit(self, projection: Projection, project_id: str, stage_label: str,
                      applicant_id: str, proposal: dict[str, Any]) -> dict[str, Any]:
        consortium = self._current_consortium(projection, project_id, stage_label)
        members = {item["investor_id"] for item in consortium}
        if applicant_id not in members:
            raise ValidationError("申请退出方不在当前联合体中")
        exit_type = proposal.get("exit_type", "default")
        if exit_type not in {"default", "breach"}:
            raise ValidationError("exit_type 只能是 default 或 breach")
        reason = self._text(proposal.get("reason", ""), "reason", 300)
        return {"exit_type": exit_type, "reason": reason}

    def _clean_consortium(self, value: Any, projection: Projection,
                          require_leader: str) -> list[dict[str, Any]]:
        if not isinstance(value, list) or not value:
            raise ValidationError("consortium 必须是非空数组")
        members = []
        total = 0.0
        seen: set[str] = set()
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise ValidationError(f"consortium[{index}] 必须是对象")
            investor_id = self._id(item.get("investor_id"),
                                   f"consortium[{index}].investor_id")
            if investor_id not in projection.investors:
                raise NotFoundError(f"联合体成员不存在: {investor_id}")
            if investor_id in seen:
                raise ValidationError(f"联合体成员重复: {investor_id}")
            seen.add(investor_id)
            shares = self._ratio(item.get("shares"), f"consortium[{index}].shares")
            members.append({"investor_id": investor_id, "shares": shares,
                            "controller": bool(item.get("controller")),
                            "ownership_kind": projection.investors[investor_id]["ownership_kind"]})
            total += shares
        if require_leader not in seen:
            raise ValidationError("申请人必须是联合体牵头成员")
        if abs(total - 1) > 1e-6:
            raise ValidationError("联合体持股比例之和必须为 1")
        controllers = [item["investor_id"] for item in members if item["controller"]]
        if len(controllers) != 1:
            raise ValidationError("联合体必须指定唯一控股方")
        return members

    def _clean_evidence(self, value: Any, projection: Projection) -> list[str]:
        if not isinstance(value, list):
            raise ValidationError("evidence_ids 必须是数组")
        ids = [self._id(item, "evidence_ids[]") for item in value]
        if len(set(ids)) != len(ids):
            raise ValidationError("证据引用不能重复")
        for evidence_id in ids:
            if evidence_id not in projection.evidence:
                raise NotFoundError(f"证据不存在: {evidence_id}")
        return ids

    def _clean_guarantees(self, value: Any, projection: Projection) -> list[str]:
        if not isinstance(value, list):
            raise ValidationError("guarantee_ids 必须是数组")
        ids = [self._id(item, "guarantee_ids[]") for item in value]
        if len(set(ids)) != len(ids):
            raise ValidationError("担保引用不能重复")
        for guarantee_id in ids:
            guarantee = projection.guarantees.get(guarantee_id)
            if guarantee is None:
                raise NotFoundError(f"担保不存在: {guarantee_id}")
            if guarantee["status"] != "registered":
                raise ConflictError(f"担保已被占用: {guarantee_id}")
        return ids

    def _evidence_hashes(self, projection: Projection,
                         evidence_ids: list[str]) -> dict[str, str]:
        return {eid: projection.evidence[eid]["content_hash"] for eid in evidence_ids}

    def _active_qualifications(self, projection: Projection, project_id: str,
                               stage_label: str) -> list[dict[str, Any]]:
        result = []
        awarded = projection.awarded_review(project_id, stage_label, "qualification")
        if awarded is not None:
            result.append(awarded)
        for review in projection.reviews.values():
            if (review["status"] == "pending" and review["project_id"] == project_id
                    and review["task_type"] == "qualification"
                    and review["proposal"].get("stage_label") == stage_label):
                result.append(review)
        return result

    def _competing_pending(self, projection: Projection, project_id: str, stage_label: str,
                           task_type: str, review_id: str) -> list[str]:
        if task_type not in {"qualification", "entry"}:
            return []
        return [r["review_id"] for r in projection.pending_reviews()
                if r["project_id"] == project_id and r["task_type"] == task_type
                and r["proposal"].get("stage_label") == stage_label
                and r["review_id"] != review_id]

    def _occupy_assets(self, connection, projection: Projection, *, project_id: str,
                       review_id: str, investor_id: str, assets: list[str],
                       request_id: str, actor_id: str, reason: str) -> None:
        for asset_key in assets:
            active = projection.assets.get(asset_key)
            if active is not None and active["status"] in {"reserved", "committed"}:
                if active["project_id"] != project_id:
                    raise ConflictError(f"资产 {asset_key} 已被其他项目承诺，禁止重复占用")
                if active["review_id"] != review_id:
                    raise ConflictError(f"资产 {asset_key} 已被项目内另一在途申请占用")
            self._emit(connection, aggregate_type="asset", aggregate_id=asset_key,
                       event_type="asset.occupied",
                       payload={"asset_key": asset_key, "project_id": project_id,
                                "review_id": review_id, "investor_id": investor_id,
                                "status": "reserved", "reason": reason},
                       actor_id=actor_id, request_id=request_id)
            projection.assets[asset_key] = {
                "asset_key": asset_key, "project_id": project_id, "review_id": review_id,
                "investor_id": investor_id, "status": "reserved", "reason": reason,
                "at": self._now()}

    # -- 联合体当前状态（从历次生效协议折叠）------------------------------------

    def _awarded_for_project(self, projection: Projection, project_id: str,
                             stage_label: str) -> list[dict[str, Any]]:
        reviews = [r for r in projection.reviews.values()
                   if r["project_id"] == project_id and r["status"] == "approved"
                   and r.get("sealed_agreement")]
        reviews.sort(key=lambda r: r["sealed_agreement"]["sealed_at"])
        return reviews

    def _latest_awarded(self, projection: Projection, project_id: str,
                        task_type: str) -> dict[str, Any] | None:
        reviews = [r for r in projection.reviews.values()
                   if r["project_id"] == project_id and r["status"] == "approved"
                   and r["task_type"] == task_type and r.get("sealed_agreement")]
        if not reviews:
            return None
        return sorted(reviews, key=lambda r: r["sealed_agreement"]["sealed_at"])[-1]

    def _current_consortium(self, projection: Projection, project_id: str,
                            stage_label: str) -> list[dict[str, Any]]:
        awarded = self._awarded_for_project(projection, project_id, stage_label)
        if not awarded:
            return []
        exited = {r["applicant_id"] for r in projection.reviews.values()
                  if r["project_id"] == project_id and r["status"] == "exited"}
        # 最近一份生效协议（准入/成员变更/控制权变更）给出完整联合体，再剔除已退出方。
        source = awarded[-1]["sealed_agreement"].get("consortium", [])
        return [dict(item) for item in source if item["investor_id"] not in exited]

    def _current_controller(self, projection: Projection, project_id: str,
                            stage_label: str) -> str | None:
        awarded = self._awarded_for_project(projection, project_id, stage_label)
        if not awarded:
            return None
        return awarded[-1]["sealed_agreement"].get("controller")

    def _policy_of_latest(self, projection: Projection, project_id: str) -> dict[str, Any]:
        review = self._latest_awarded(projection, project_id, "qualification")
        pin = review["policy_version"]
        return projection.policies[pin["policy_id"]]["versions"][pin["version"]]

    # -- 会签意见 -------------------------------------------------------------

    def record_approval(self, *, request_id: str, actor_id: str, review_id: str,
                        decision: str, comment: str = "") -> AdmissionReceipt:
        review_id = self._id(review_id, "review_id")
        if decision not in {"approve", "reject"}:
            raise ValidationError("decision 只能是 approve 或 reject")
        comment = str(comment or "")[:300]

        def work(connection, actor) -> tuple[str, str, dict[str, Any]]:
            projection = self._projection(connection)
            review = projection.reviews.get(review_id)
            if review is None:
                raise NotFoundError("审查不存在")
            if review["status"] != "pending":
                raise ConflictError("审查已经终结，不能再签署")
            pending = projection.pending_reviews()
            head = next((item for item in pending if item["project_id"] == review["project_id"]), None)
            if head is None or head["review_id"] != review_id:
                raise ConflictError("项目存在更早的待审任务，须按队列顺序会签")
            if actor["actor_id"] == review["created_by"]:
                raise PermissionDenied("职责分离：提议人不能签署同一审查")
            approvals = review["approvals"]
            if any(item["actor_id"] == actor["actor_id"] for item in approvals):
                raise ConflictError("同一操作者不能重复会签")
            expected_role = "reviewer" if len(approvals) == 0 else "admin"
            self._require(actor, expected_role)

            approval = {"role": expected_role, "actor_id": actor["actor_id"],
                        "decision": decision, "comment": comment, "signed_at": self._now()}
            self._emit(connection, aggregate_type="review", aggregate_id=review_id,
                       event_type="review.approval_recorded",
                       payload={"review_id": review_id, "approval": approval},
                       actor_id=actor["actor_id"], request_id=request_id)

            if decision == "reject":
                self._close_review(connection, projection, review, "rejected", "rejected",
                                   comment or "会签驳回", actor["actor_id"], request_id)
                return "review", review_id, {"status": "rejected", "outcome": "rejected"}

            if len(approvals) == 0:
                return "review", review_id, {"status": "pending", "awaiting": "admin"}

            return self._finalize_approval(connection, projection, review, actor, request_id)

        return self._write(request_id=request_id, actor_id=actor_id,
                           action="record_approval",
                           payload={"actor_id": actor_id, "review_id": review_id,
                                    "decision": decision, "comment": comment},
                           allowed_roles=("reviewer", "admin"), work=work)

    def _finalize_approval(self, connection, projection: Projection, review: dict[str, Any],
                           actor, request_id: str) -> tuple[str, str, dict[str, Any]]:
        task_type = review["task_type"]
        if task_type == "exit":
            return self._settle_exit(connection, projection, review, actor, request_id)

        if task_type in {"qualification", "entry"}:
            stage_label = review["proposal"]["stage_label"]
            winner = projection.awarded_review(review["project_id"], stage_label, task_type)
            if winner is not None and task_type == winner["task_type"]:
                self._close_review(connection, projection, review, "closed", "lost",
                                   "竞争申请已有生效结果", actor["actor_id"], request_id)
                return "review", review["review_id"], {"status": "closed", "outcome": "lost"}
            agreement = self._seal_agreement(projection, review, actor["actor_id"])
            self._commit_review_assets(connection, projection, review, actor["actor_id"],
                                       request_id)
            self._commit_guarantees(connection, projection, review, actor["actor_id"],
                                    request_id)
            self._decide(connection, review, status="approved", outcome="awarded",
                         message="会签通过并授标", actor_id=actor["actor_id"],
                         request_id=request_id, sealed_agreement=agreement)
            for competitor in projection.pending_reviews():
                if (competitor["project_id"] == review["project_id"]
                        and competitor["task_type"] == task_type
                        and competitor["proposal"].get("stage_label") == stage_label
                        and competitor["review_id"] != review["review_id"]):
                    self._close_review(connection, projection, competitor, "closed", "lost",
                                       f"竞争申请 {review['review_id']} 已生效",
                                       actor["actor_id"], request_id)
            return "review", review["review_id"], {"status": "approved", "outcome": "awarded",
                                                   "agreement_id": agreement["agreement_id"]}

        agreement = self._seal_agreement(projection, review, actor["actor_id"])
        if task_type == "member_change":
            self._commit_review_assets(connection, projection, review, actor["actor_id"],
                                       request_id)
        self._decide(connection, review, status="approved", outcome=task_type,
                     message="会签通过", actor_id=actor["actor_id"], request_id=request_id,
                     sealed_agreement=agreement)
        return "review", review["review_id"], {"status": "approved", "outcome": task_type,
                                               "agreement_id": agreement["agreement_id"]}

    def _seal_agreement(self, projection: Projection, review: dict[str, Any],
                        signer: str) -> dict[str, Any]:
        project = projection.projects[review["project_id"]]
        project_payload = project["versions"][review["project_version"]]
        policy_pin = review["policy_version"]
        policy_payload = (projection.policies[policy_pin["policy_id"]]["versions"]
                          [policy_pin["version"]])
        proposal = review["proposal"]
        if review["task_type"] == "qualification":
            consortium = proposal["consortium"]
            controller = proposal["controller"]
        elif review["task_type"] == "entry":
            prior = self._latest_awarded(projection, review["project_id"], "qualification")
            consortium = prior["sealed_agreement"]["consortium"]
            controller = prior["sealed_agreement"]["controller"]
        elif review["task_type"] == "member_change":
            consortium = proposal["resulting_members"]
            controllers = [c["investor_id"] for c in consortium if c.get("controller")]
            controller = controllers[0] if controllers else \
                self._current_controller(projection, review["project_id"],
                                         proposal["stage_label"])
        else:  # control_change
            consortium = [{"investor_id": investor_id, "shares": shares,
                           "controller": investor_id == proposal["new_controller"]}
                          for investor_id, shares in proposal["holdings"].items()]
            controller = proposal["new_controller"]

        evidence = [{"evidence_id": eid,
                     "content_hash": projection.evidence[eid]["content_hash"],
                     "evidence_type": projection.evidence[eid]["evidence_type"],
                     "verified": projection.evidence[eid]["verified"]}
                    for eid in proposal.get("evidence_ids", [])]
        plan = None
        plan_hash = None
        if proposal.get("capital_plan_id"):
            plan = projection.capital_plans[proposal["capital_plan_id"]]
            plan_hash = digest({k: plan[k] for k in
                                ("plan_id", "total_amount", "private_amount",
                                 "private_ratio", "tranches")})
        agreement = {
            "agreement_id": uuid.uuid4().hex,
            "review_id": review["review_id"],
            "project_id": review["project_id"],
            "task_type": review["task_type"],
            "stage_label": proposal.get("stage_label", DEFAULT_STAGE),
            "consortium": consortium,
            "controller": controller,
            "policy_version": {"policy_id": policy_payload["policy_id"],
                               "version": policy_payload["version"],
                               "thresholds": policy_payload["thresholds"],
                               "thresholds_hash": digest(policy_payload["thresholds"])},
            "project_version": review["project_version"],
            "project_snapshot_hash": digest({k: project_payload[k] for k in
                                             ("project_id", "version", "total_investment",
                                              "private_target_ratio", "assets")}),
            "evidence": evidence,
            "capital_plan_id": proposal.get("capital_plan_id"),
            "capital_plan_hash": plan_hash,
            "capital_plan_summary": None if plan is None else {
                "total_amount": plan["total_amount"], "private_amount": plan["private_amount"],
                "private_ratio": plan["private_ratio"]},
            "guarantee_ids": proposal.get("guarantee_ids", []),
            "approvals": [dict(item) for item in review["approvals"]],
            "sealed_by": signer,
            "sealed_at": self._now(),
        }
        agreement["agreement_hash"] = digest(
            {k: v for k, v in agreement.items() if k != "agreement_hash"})
        return agreement

    def _decide(self, connection, review: dict[str, Any], *, status: str, outcome: str,
                message: str, actor_id: str, request_id: str,
                sealed_agreement: dict[str, Any] | None = None,
                settlement: dict[str, Any] | None = None) -> None:
        payload = {"review_id": review["review_id"], "status": status, "outcome": outcome,
                   "message": message, "decided_at": self._now()}
        if sealed_agreement is not None:
            payload["sealed_agreement"] = sealed_agreement
        if settlement is not None:
            payload["settlement"] = settlement
        self._emit(connection, aggregate_type="review", aggregate_id=review["review_id"],
                   event_type="review.exit_settled" if settlement is not None
                   else "review.decided", payload=payload,
                   actor_id=actor_id, request_id=request_id)
        review["status"] = status
        review["outcome"] = outcome
        review["message"] = message
        review["decided_at"] = payload["decided_at"]
        if sealed_agreement is not None:
            review["sealed_agreement"] = sealed_agreement
        if settlement is not None:
            review["settlement"] = settlement

    def _close_review(self, connection, projection: Projection, review: dict[str, Any],
                      status: str, outcome: str, message: str, actor_id: str,
                      request_id: str) -> None:
        self._release_assets(connection, projection, review["review_id"],
                             actor_id, request_id, reason=message)
        self._decide(connection, review, status=status, outcome=outcome, message=message,
                     actor_id=actor_id, request_id=request_id)

    def _release_assets(self, connection, projection: Projection, review_id: str,
                        actor_id: str, request_id: str, *, reason: str,
                        asset_keys: list[str] | None = None) -> list[str]:
        keys = asset_keys or [key for key, item in projection.assets.items()
                              if item["review_id"] == review_id
                              and item["status"] in {"reserved", "committed"}]
        for key in keys:
            item = projection.assets[key]
            if item["status"] not in {"reserved", "committed"}:
                continue
            self._emit(connection, aggregate_type="asset", aggregate_id=key,
                       event_type="asset.released",
                       payload={"asset_key": key, "review_id": review_id, "reason": reason},
                       actor_id=actor_id, request_id=request_id)
            item["status"] = "released"
            item["reason"] = reason
        return keys

    def _commit_review_assets(self, connection, projection: Projection, review: dict[str, Any],
                              actor_id: str, request_id: str) -> None:
        for key in review["proposal"].get("assets", []):
            item = projection.assets.get(key)
            if item is None or item["review_id"] != review["review_id"]:
                continue
            self._emit(connection, aggregate_type="asset", aggregate_id=key,
                       event_type="asset.occupied",
                       payload={"asset_key": key, "project_id": review["project_id"],
                                "review_id": review["review_id"],
                                "investor_id": review["applicant_id"],
                                "status": "committed", "reason": "会签授标承诺"},
                       actor_id=actor_id, request_id=request_id)
            item["status"] = "committed"

    def _commit_guarantees(self, connection, projection: Projection, review: dict[str, Any],
                          actor_id: str, request_id: str) -> None:
        for guarantee_id in review["proposal"].get("guarantee_ids", []):
            guarantee = projection.guarantees[guarantee_id]
            if guarantee["status"] != "registered":
                continue
            self._emit(connection, aggregate_type="guarantee", aggregate_id=guarantee_id,
                       event_type="guarantee.status_changed",
                       payload={"guarantee_id": guarantee_id, "status": "committed",
                                "amount": guarantee["amount"],
                                "reason": f"审查 {review['review_id']} 会签通过"},
                       actor_id=actor_id, request_id=request_id)
            guarantee["status"] = "committed"

    # -- 退出结清 -------------------------------------------------------------

    def _settle_exit(self, connection, projection: Projection, review: dict[str, Any],
                     actor, request_id: str) -> tuple[str, str, dict[str, Any]]:
        project_id = review["project_id"]
        investor_id = review["applicant_id"]
        stage_label = review["proposal"]["stage_label"]
        exit_type = review["proposal"]["exit_type"]
        qualification = self._latest_awarded(projection, project_id, "qualification")
        agreement = qualification["sealed_agreement"]
        current_members = self._current_consortium(projection, project_id, stage_label)
        member = next((m for m in current_members
                       if m["investor_id"] == investor_id), None)
        if member is None:
            member = next((m for m in agreement["consortium"]
                           if m["investor_id"] == investor_id), None)
        if member is None:
            raise ValidationError("退出方不在生效协议联合体中")
        plan = projection.capital_plans[agreement["capital_plan_id"]]
        committed_capital = round(plan["total_amount"] * member["shares"], 2)
        paid_capital = round(sum(
            o["amount"] for o in projection.obligations.values()
            if o["project_id"] == project_id and o["investor_id"] == investor_id
            and o["obligation_type"] == "capital_payment" and o["status"] == "paid"), 2)
        outstanding_capital = round(committed_capital - paid_capital, 2)
        unfinished = [{"obligation_id": o["obligation_id"],
                       "obligation_type": o["obligation_type"],
                       "description": o["description"], "amount": o["amount"],
                       "due_at": o["due_at"], "status": o["status"]}
                      for o in projection.obligations.values()
                      if o["project_id"] == project_id and o["investor_id"] == investor_id
                      and o["status"] in {"pending", "in_progress"}]
        # 出资类义务的欠缴已体现在 outstanding_capital 中，此处只累加非出资类未完义务，避免重复追索。
        non_capital_cost = round(sum(item["amount"] for item in unfinished
                                     if item["obligation_type"] != "capital_payment"), 2)
        recourse_amount = round(outstanding_capital + non_capital_cost, 2)

        guarantee_ids = set()
        for awarded in self._awarded_for_project(projection, project_id, stage_label):
            guarantee_ids.update(awarded["sealed_agreement"].get("guarantee_ids", []))
        eligible = [projection.guarantees[gid] for gid in sorted(guarantee_ids)
                    if projection.guarantees[gid]["investor_id"] == investor_id
                    and projection.guarantees[gid]["status"] == "committed"]
        total_guarantee = round(sum(item["amount"] for item in eligible), 2)
        if exit_type == "breach":
            forfeited_amount = total_guarantee
        else:
            forfeited_amount = round(min(total_guarantee, recourse_amount), 2)
        refunded_amount = round(total_guarantee - forfeited_amount, 2)
        guarantee_results = []
        for item in eligible:
            share_forfeit = round(min(item["amount"],
                                      max(0.0, forfeited_amount
                                          - sum(g["forfeited_amount"] for g in guarantee_results))), 2)
            status = "forfeited" if share_forfeit > 0 else "returned"
            self._emit(connection, aggregate_type="guarantee",
                       aggregate_id=item["guarantee_id"],
                       event_type="guarantee.status_changed",
                       payload={"guarantee_id": item["guarantee_id"], "status": status,
                                "amount": share_forfeit,
                                "reason": f"退出结清({exit_type})"},
                       actor_id=actor["actor_id"], request_id=request_id)
            item["status"] = status
            guarantee_results.append({"guarantee_id": item["guarantee_id"],
                                      "amount": item["amount"],
                                      "forfeited_amount": share_forfeit,
                                      "refunded_amount": round(item["amount"] - share_forfeit, 2),
                                      "status": status})

        released = self._release_assets(
            connection, projection, review["review_id"], actor["actor_id"], request_id,
            reason=f"退出结清({exit_type})",
            asset_keys=[key for key, item in projection.assets.items()
                        if item["project_id"] == project_id
                        and item["investor_id"] == investor_id
                        and item["status"] == "committed"])
        settlement = {
            "review_id": review["review_id"], "project_id": project_id,
            "investor_id": investor_id, "exit_type": exit_type,
            "paid_capital": paid_capital, "committed_capital": committed_capital,
            "outstanding_capital": outstanding_capital,
            "unfinished_obligations": unfinished, "unfinished_cost": non_capital_cost,
            "recourse_amount": recourse_amount, "guarantees": guarantee_results,
            "forfeited_amount": forfeited_amount, "refunded_amount": refunded_amount,
            "released_assets": released, "settled_at": self._now()}
        self._decide(connection, review, status="exited", outcome=f"exit_{exit_type}",
                     message="退出结清完成", actor_id=actor["actor_id"],
                     request_id=request_id, settlement=settlement)
        return "review", review["review_id"], {"status": "exited",
                                               "outcome": f"exit_{exit_type}",
                                               "recourse_amount": recourse_amount,
                                               "forfeited_amount": forfeited_amount,
                                               "refunded_amount": refunded_amount}

    # ------------------------------------------------------------------ 查询

    def get_review(self, review_id: str, as_of: str | None = None) -> ReviewTask:
        events = self.store.read_events(aggregate_type="review", aggregate_id=review_id,
                                        as_of=as_of)
        projection = build_projection(events)
        review = projection.reviews.get(review_id)
        if review is None:
            raise NotFoundError("审查不存在")
        return self._to_review_task(review, projection.review_sequence.get(review_id, 0))

    def _to_review_task(self, review: dict[str, Any], sequence: int) -> ReviewTask:
        approvals = tuple(Approval(item["role"], item["actor_id"], item["decision"],
                                   item["comment"], item["signed_at"])
                          for item in review.get("approvals", []))
        return ReviewTask(
            review_id=review["review_id"], project_id=review["project_id"],
            task_type=review["task_type"], applicant_id=review["applicant_id"],
            status=review["status"], policy_version=review["policy_version"]["version"],
            project_version=review["project_version"], proposal=review["proposal"],
            evidence_hashes=review.get("evidence_hashes", {}), approvals=approvals,
            competing=tuple(review.get("competing", [])), outcome=review.get("outcome", ""),
            sealed_agreement=review.get("sealed_agreement"), sequence=sequence,
            created_at=review["created_at"], created_by=review["created_by"],
            decided_at=review.get("decided_at"), message=review.get("message", ""),
            settlement=review.get("settlement"))

    def pending_queue(self) -> list[dict[str, Any]]:
        projection = self._projection(self.database.connection)
        rows = self.store.read_events()
        sequences = {event["aggregate_id"]: event["sequence"]
                     for event in rows if event["event_type"] == "review.proposed"}
        items = []
        for position, review in enumerate(projection.pending_reviews(), start=1):
            task = self._to_review_task(review, sequences[review["review_id"]])
            items.append({"position": position, **asdict(task)})
        return items

    def compare_scenarios(self, project_id: str,
                          stage_label: str = DEFAULT_STAGE) -> dict[str, Any]:
        projection = self._projection(self.database.connection)
        if project_id not in projection.projects:
            raise NotFoundError("项目不存在")
        candidates = []
        for review in projection.reviews.values():
            if (review["project_id"] == project_id and review["status"] == "pending"
                    and review["task_type"] == "qualification"
                    and review["proposal"].get("stage_label") == stage_label):
                candidates.append(self._score_candidate(projection, review))
        awarded = projection.awarded_review(project_id, stage_label, "qualification")
        if awarded is not None:
            candidates.append(self._score_candidate(projection, awarded, awarded=True))
        candidates.sort(key=lambda item: (item["delivery_risk"], -item["private_ratio"]))
        for rank, item in enumerate(candidates, start=1):
            item["rank"] = rank
        return {"project_id": project_id, "stage_label": stage_label,
                "candidates": candidates,
                "recommended": candidates[0]["review_id"] if candidates else None}

    def _score_candidate(self, projection: Projection, review: dict[str, Any],
                         awarded: bool = False) -> dict[str, Any]:
        proposal = review["proposal"]
        pin = review["policy_version"]
        thresholds = (projection.policies[pin["policy_id"]]["versions"]
                      [pin["version"]]["thresholds"])
        plan = projection.capital_plans[proposal["capital_plan_id"]]
        guarantees = [projection.guarantees[gid] for gid in proposal["guarantee_ids"]]
        coverage = sum(item["amount"] for item in guarantees) / plan["total_amount"]
        evidence = [projection.evidence[eid] for eid in proposal["evidence_ids"]]
        verified_ratio = (sum(1 for e in evidence if e["verified"]) / len(evidence)) if evidence else 0
        top_share = max(item["shares"] for item in proposal["consortium"])
        first_tranche = plan["tranches"][0]["amount"] / plan["total_amount"]
        present_types = {item["evidence_type"] for item in evidence}
        missing_recommended = sorted(set(thresholds["recommended_evidence_types"]) - present_types)

        risk = 10.0
        min_cov = thresholds["min_guarantee_ratio"]
        if min_cov:
            risk += 30.0 * max(0.0, (min_cov - coverage) / min_cov)
        if plan["private_ratio"] < thresholds["min_private_ratio"] + 0.05:
            risk += 15.0
        if top_share > thresholds["max_controller_share"]:
            risk += 15.0
        if first_tranche < 0.2:
            risk += 10.0
        risk += 15.0 * (1.0 - verified_ratio)
        risk += min(20.0, 10.0 * len(missing_recommended))
        risk = round(min(100.0, risk), 2)
        return {
            "review_id": review["review_id"], "applicant_id": review["applicant_id"],
            "status": review["status"], "awarded": awarded,
            "private_ratio": plan["private_ratio"],
            "private_amount": plan["private_amount"],
            "total_amount": plan["total_amount"],
            "guarantee_coverage": round(coverage, 6),
            "evidence_verified_ratio": round(verified_ratio, 6),
            "top_controller_share": top_share,
            "missing_recommended_evidence": missing_recommended,
            "delivery_risk": risk,
        }

    def snapshot(self, project_id: str, as_of: str | None = None) -> dict[str, Any]:
        projection = self._projection(self.database.connection, as_of=as_of)
        if project_id not in projection.projects:
            raise NotFoundError("项目在该时点不存在")
        project = projection.projects[project_id]
        versions = sorted(project["versions"].values(), key=lambda item: item["version"])
        current = versions[-1]
        reviews = []
        policies_used: dict[str, set[int]] = {}
        for review in sorted((r for r in projection.reviews.values()
                              if r["project_id"] == project_id),
                             key=lambda r: projection.review_sequence[r["review_id"]]):
            task = self._to_review_task(
                review, projection.review_sequence[review["review_id"]])
            reviews.append(asdict(task))
            pin = review["policy_version"]
            policies_used.setdefault(pin["policy_id"], set()).add(pin["version"])
        assets = [item for item in projection.assets.values()
                  if item["project_id"] == project_id]
        review_projects = {
            event["aggregate_id"]: event["payload"].get("project_id")
            for event in self.store.read_events(as_of=as_of)
            if event["event_type"] == "review.proposed"}
        timeline = [{"sequence": event["sequence"], "event_id": event["event_id"],
                     "aggregate_type": event["aggregate_type"],
                     "aggregate_id": event["aggregate_id"],
                     "event_type": event["event_type"], "actor_id": event["actor_id"],
                     "created_at": event["created_at"],
                     "payload_hash": digest(event["payload"])}
                    for event in self.store.read_events(as_of=as_of)
                    if event["aggregate_id"] == project_id
                    or event["payload"].get("project_id") == project_id
                    or (event["aggregate_type"] == "review"
                        and review_projects.get(event["aggregate_id"]) == project_id)]
        last_sequence = timeline[-1]["sequence"] if timeline else 0
        return {
            "project_id": project_id, "as_of": as_of,
            "last_sequence": last_sequence,
            "project_version": {"version": current["version"], "name": current["name"],
                                "total_investment": current["total_investment"],
                                "private_target_ratio": current["private_target_ratio"],
                                "assets": current["assets"],
                                "snapshot_hash": digest(current)},
            "all_versions": [{"version": item["version"], "published_at": item["published_at"]}
                             for item in versions],
            "policies_used": {pid: sorted(vers) for pid, vers in policies_used.items()},
            "reviews": reviews,
            "assets": assets,
            "timeline": timeline,
        }
