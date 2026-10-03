"""提供交通项目社会资本准入与退出的统一审查服务。

覆盖规则版本、投资人关联关系、资格证据、出资计划、履约担保、
职责分离会签、竞争申请唯一成交、控制权变更、违约退出结算、
资产防重复承诺、时点还原与候选方案比较。
"""

from __future__ import annotations

import uuid
from typing import Any

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .storage import Database


INVESTOR_KINDS = frozenset({"private", "state_owned", "mixed", "foreign"})
RELATION_KINDS = frozenset({"parent", "subsidiary", "actual_control", "affiliate", "consortium"})
EVIDENCE_KINDS = frozenset({"business_license", "qualification", "credit", "financial", "safety"})
TASK_KIND_ROLES = {
    "admission": ("reviewer", "admin"),
    "member_add": ("reviewer", "admin"),
    "member_remove": ("reviewer", "admin"),
    "phased_entry": ("reviewer", "admin"),
    "control_change": ("reviewer", "admin"),
    "default_exit": ("reviewer", "admin"),
}
MEMBER_CHANGE_KINDS = frozenset({"member_add", "phased_entry"})


class ParticipationService:
    """协调规则、申请、会签与退出结算的领域服务。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _actor(self, connection, actor_id: str):
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require_roles(self, actor, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _replay(self, connection, *, request_id: str, action: str,
                payload: dict[str, Any]) -> dict[str, Any] | None:
        """若 request_id 已处理则返回回放结果，否则返回 None。"""

        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        import json as _json
        return {**_json.loads(row["response_json"]), "replayed": True}

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create) -> dict[str, Any]:
        replay = self._replay(connection, request_id=request_id, action=action, payload=payload)
        if replay is not None:
            return replay
        result = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, digest(payload), result["resource_type"], result["resource_id"],
             canonical_json({k: v for k, v in result.items() if k != "replayed"}), self._now()),
        )
        return {**result, "replayed": False}

    def _nextval(self, connection, name: str) -> int:
        connection.execute(
            "INSERT INTO counters(counter,value) VALUES(?,1) ON CONFLICT(counter) "
            "DO UPDATE SET value=value+1", (name,),
        )
        return connection.execute("SELECT value FROM counters WHERE counter=?", (name,)).fetchone()["value"]

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    # ------------------------------------------------------------------
    # 规则版本：换版只对未来生效，已签协议固定在旧版
    # ------------------------------------------------------------------

    def publish_rule_version(self, *, request_id: str, actor_id: str, rules: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(rules, dict) or not rules:
            raise ValidationError("rules 必须是非空对象")
        self._validate_rules(rules)
        payload = {"actor_id": actor_id, "rules": rules}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin")

            def create():
                ordinal = self._nextval(connection, "rule_version")
                version_id = f"rules-{ordinal:04d}"
                effective_at = self._now()
                connection.execute(
                    "INSERT INTO rule_versions(version_id,ordinal,rules_json,content_hash,"
                    "effective_at,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (version_id, ordinal, canonical_json(rules), digest(rules),
                     effective_at, actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="rules.published",
                            resource_type="rule_version", resource_id=version_id,
                            detail={"ordinal": ordinal, "content_hash": digest(rules)})
                return {"resource_type": "rule_version", "resource_id": version_id,
                        "version_id": version_id, "ordinal": ordinal,
                        "effective_at": effective_at, "content_hash": digest(rules)}

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_rule_version", payload=payload, create=create)

    def _validate_rules(self, rules: dict[str, Any]) -> None:
        ratio = rules.get("min_private_ratio")
        if not isinstance(ratio, (int, float)) or not 0 <= ratio <= 1:
            raise ValidationError("rules.min_private_ratio 必须是 0 到 1 之间的数字")
        kinds = rules.get("required_evidence_kinds", [])
        if not isinstance(kinds, list) or not all(isinstance(k, str) for k in kinds):
            raise ValidationError("rules.required_evidence_kinds 必须是字符串数组")
        rates = rules.get("guarantee_rates", {})
        if not isinstance(rates, dict):
            raise ValidationError("rules.guarantee_rates 必须是对象")
        for key, value in rates.items():
            if key not in ("bid", "performance") or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise ValidationError("rules.guarantee_rates 只能包含 0 到 1 的 bid/performance 比率")

    def _current_rule(self, connection):
        row = connection.execute(
            "SELECT * FROM rule_versions ORDER BY ordinal DESC LIMIT 1").fetchone()
        if row is None:
            raise ConflictError("尚未发布任何规则版本")
        return row

    def _load_rules(self, connection, version_id: str):
        import json
        row = connection.execute("SELECT * FROM rule_versions WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise NotFoundError("规则版本不存在")
        return row, json.loads(row["rules_json"])

    # ------------------------------------------------------------------
    # 投资人及关联关系
    # ------------------------------------------------------------------

    def register_investor(self, *, request_id: str, actor_id: str,
                          investor_id: str, name: str, kind: str,
                          is_private: bool | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "investor_id": investor_id, "name": name, "kind": kind}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator")
            investor_id = str(investor_id).strip()
            name = str(name).strip()
            if not investor_id or not name:
                raise ValidationError("investor_id 与 name 不能为空")
            if kind not in INVESTOR_KINDS:
                raise ValidationError("kind 不在允许范围内")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO investors(investor_id,name,kind,created_by,created_at) VALUES(?,?,?,?,?)",
                        (investor_id, name, kind, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("投资人编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="investor.registered",
                            resource_type="investor", resource_id=investor_id,
                            detail={"name": name, "kind": kind, "is_private": bool(is_private)})
                return {"resource_type": "investor", "resource_id": investor_id,
                        "investor_id": investor_id, "is_private": bool(is_private)}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_investor", payload=payload, create=create)

    def register_relation(self, *, request_id: str, actor_id: str, relation_id: str,
                          from_investor: str, to_investor: str,
                          relation_kind: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "from_investor": from_investor,
                   "to_investor": to_investor, "relation_kind": relation_kind}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator")
            if relation_kind not in RELATION_KINDS:
                raise ValidationError("relation_kind 不在允许范围内")
            if from_investor == to_investor:
                raise ValidationError("关联关系两端必须是不同主体")
            for investor_id in (from_investor, to_investor):
                if connection.execute("SELECT 1 FROM investors WHERE investor_id=?",
                                      (investor_id,)).fetchone() is None:
                    raise NotFoundError(f"投资人 {investor_id} 不存在")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO investor_relations(relation_id,from_investor,to_investor,"
                        "relation_kind,effective_at,created_by) VALUES(?,?,?,?,?,?)",
                        (relation_id, from_investor, to_investor, relation_kind,
                         self._now(), actor_id),
                    )
                except Exception as exc:
                    raise ConflictError("关系编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="relation.registered",
                            resource_type="relation", resource_id=relation_id,
                            detail={"from": from_investor, "to": to_investor, "kind": relation_kind})
                return {"resource_type": "relation", "resource_id": relation_id,
                        "relation_id": relation_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_relation", payload=payload, create=create)

    def revoke_relation(self, *, request_id: str, actor_id: str, relation_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "relation_id": relation_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator")
            row = connection.execute(
                "SELECT * FROM investor_relations WHERE relation_id=?", (relation_id,)).fetchone()
            if row is None:
                raise NotFoundError("关联关系不存在")
            if row["revoked_at"]:
                raise ConflictError("关联关系已经撤销")

            def create():
                connection.execute(
                    "UPDATE investor_relations SET revoked_at=? WHERE relation_id=?",
                    (self._now(), relation_id))
                self._audit(connection, actor_id=actor_id, action="relation.revoked",
                            resource_type="relation", resource_id=relation_id, detail={})
                return {"resource_type": "relation", "resource_id": relation_id,
                        "relation_id": relation_id, "revoked_at": self._now()}

            return self._idempotent(connection, request_id=request_id,
                                    action="revoke_relation", payload=payload, create=create)

    def _related_groups(self, connection) -> dict[str, int]:
        """对当前生效的关联关系做并查集分组。"""

        parent: dict[str, str] = {}

        def find(x: str) -> str:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        rows = connection.execute(
            "SELECT from_investor,to_investor FROM investor_relations WHERE revoked_at IS NULL").fetchall()
        for row in rows:
            union(row["from_investor"], row["to_investor"])
        groups: dict[str, int] = {}
        roots: dict[str, int] = {}
        for node in list(parent):
            root = find(node)
            if root not in roots:
                roots[root] = len(roots) + 1
            groups[node] = roots[root]
        return groups

    # ------------------------------------------------------------------
    # 承诺资产：同一资产在任一时刻只能被一个项目占用
    # ------------------------------------------------------------------

    def register_asset(self, *, request_id: str, actor_id: str, asset_id: str,
                       investor_id: str, name: str, assessed_value: float) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "asset_id": asset_id, "investor_id": investor_id,
                   "assessed_value": assessed_value}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator")
            if not isinstance(assessed_value, (int, float)) or assessed_value <= 0:
                raise ValidationError("assessed_value 必须为正数")
            if connection.execute("SELECT 1 FROM investors WHERE investor_id=?",
                                  (investor_id,)).fetchone() is None:
                raise NotFoundError("投资人不存在")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO pledged_assets(asset_id,investor_id,name,assessed_value,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?)",
                        (asset_id, investor_id, str(name), float(assessed_value), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("资产编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="asset.registered",
                            resource_type="asset", resource_id=asset_id,
                            detail={"investor_id": investor_id, "assessed_value": float(assessed_value)})
                return {"resource_type": "asset", "resource_id": asset_id, "asset_id": asset_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_asset", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 资格证据：登记、核验、吊销
    # ------------------------------------------------------------------

    def register_evidence(self, *, request_id: str, actor_id: str, evidence_id: str,
                          investor_id: str, evidence_kind: str, document_hash: str,
                          issued_at: str, expires_at: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "evidence_id": evidence_id, "investor_id": investor_id,
                   "evidence_kind": evidence_kind, "document_hash": document_hash,
                   "issued_at": issued_at, "expires_at": expires_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator")
            if evidence_kind not in EVIDENCE_KINDS:
                raise ValidationError("evidence_kind 不在允许范围内")
            document_hash = str(document_hash).strip()
            if len(document_hash) < 8:
                raise ValidationError("document_hash 长度不足")
            if connection.execute("SELECT 1 FROM investors WHERE investor_id=?",
                                  (investor_id,)).fetchone() is None:
                raise NotFoundError("投资人不存在")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO evidence(evidence_id,investor_id,evidence_kind,document_hash,"
                        "issued_at,expires_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (evidence_id, investor_id, evidence_kind, document_hash,
                         issued_at, expires_at, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("证据编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="evidence.registered",
                            resource_type="evidence", resource_id=evidence_id,
                            detail={"investor_id": investor_id, "kind": evidence_kind,
                                    "document_hash": document_hash})
                return {"resource_type": "evidence", "resource_id": evidence_id,
                        "evidence_id": evidence_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_evidence", payload=payload, create=create)

    def _set_evidence_status(self, connection, actor, *, evidence_id: str, status: str, note: str):
        row = connection.execute("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
        if row is None:
            raise NotFoundError("证据不存在")
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS next FROM evidence_verifications WHERE evidence_id=?",
            (evidence_id,)).fetchone()["next"]
        connection.execute(
            "INSERT INTO evidence_verifications(evidence_id,seq,status,actor_id,note,at) "
            "VALUES(?,?,?,?,?,?)",
            (evidence_id, seq, status, actor["actor_id"], note, self._now()))
        self._audit(connection, actor_id=actor["actor_id"],
                    action=f"evidence.{status}", resource_type="evidence",
                    resource_id=evidence_id, detail={"seq": seq, "note": note})

    def verify_evidence(self, *, request_id: str, actor_id: str, evidence_id: str,
                        note: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "evidence_id": evidence_id, "status": "verified"}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "reviewer", "admin")

            def create():
                self._set_evidence_status(connection, actor, evidence_id=evidence_id,
                                          status="verified", note=note)
                return {"resource_type": "evidence", "resource_id": evidence_id,
                        "evidence_id": evidence_id, "status": "verified"}

            return self._idempotent(connection, request_id=request_id,
                                    action="verify_evidence", payload=payload, create=create)

    def revoke_evidence(self, *, request_id: str, actor_id: str, evidence_id: str,
                        note: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "evidence_id": evidence_id, "status": "revoked"}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "reviewer", "admin")

            def create():
                self._set_evidence_status(connection, actor, evidence_id=evidence_id,
                                          status="revoked", note=note)
                return {"resource_type": "evidence", "resource_id": evidence_id,
                        "evidence_id": evidence_id, "status": "revoked"}

            return self._idempotent(connection, request_id=request_id,
                                    action="revoke_evidence", payload=payload, create=create)

    def _evidence_state(self, connection, evidence_id: str):
        row = connection.execute("SELECT * FROM evidence WHERE evidence_id=?",
                                 (evidence_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"证据 {evidence_id} 不存在")
        latest = connection.execute(
            "SELECT status FROM evidence_verifications WHERE evidence_id=? ORDER BY seq DESC LIMIT 1",
            (evidence_id,)).fetchone()
        status = latest["status"] if latest else None
        valid = (status == "verified"
                 and (row["expires_at"] is None or row["expires_at"] > self._now()))
        return row, status, valid

    # ------------------------------------------------------------------
    # 项目与项目版本
    # ------------------------------------------------------------------

    def create_project(self, *, request_id: str, actor_id: str, project_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "project_id": project_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO projects(project_id,current_version,status,created_at) VALUES(?,1,'open',?)",
                        (project_id, self._now()))
                except Exception as exc:
                    raise ConflictError("项目编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="project.created",
                            resource_type="project", resource_id=project_id, detail={})
                return {"resource_type": "project", "resource_id": project_id,
                        "project_id": project_id, "version": 1}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_project", payload=payload, create=create)

    def publish_project_version(self, *, request_id: str, actor_id: str, project_id: str,
                                name: str, detail: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(detail, dict) or not detail:
            raise ValidationError("detail 必须是非空对象")
        payload = {"actor_id": actor_id, "project_id": project_id, "name": name, "detail": detail}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator")
            project = connection.execute("SELECT * FROM projects WHERE project_id=?",
                                         (project_id,)).fetchone()
            if project is None:
                raise NotFoundError("项目不存在")

            def create():
                version_no = project["current_version"] + 1
                rule = self._current_rule(connection)
                connection.execute(
                    "INSERT INTO project_versions(project_id,version_no,name,detail_json,"
                    "payload_hash,rule_version_id,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (project_id, version_no, str(name), canonical_json(detail), digest(detail),
                     rule["version_id"], actor["actor_id"], self._now()))
                connection.execute("UPDATE projects SET current_version=? WHERE project_id=?",
                                   (version_no, project_id))
                # 新版本发布后，在审申请全部失效，避免旧版本产生成交结果
                pending = connection.execute(
                    "SELECT application_id,app_seq FROM applications WHERE project_id=? AND status='submitted'",
                    (project_id,)).fetchall()
                for item in pending:
                    connection.execute("UPDATE applications SET status='rejected' WHERE application_id=?",
                                       (item["application_id"],))
                    connection.execute(
                        "INSERT INTO application_events(application_id,seq,status,actor_id,note,at) "
                        "VALUES(?,?,?,?,?,?)",
                        (item["application_id"], self._app_next_seq(connection, item["application_id"]),
                         "rejected", actor["actor_id"], "项目发布新版本，在审申请自动失效", self._now()))
                    # 旧版申请失效：释放预占资产与投标担保，供重新提交使用
                    connection.execute(
                        "UPDATE asset_engagements SET released_at=? WHERE application_id=? "
                        "AND released_at IS NULL", (self._now(), item["application_id"]))
                    self._release_guarantees(connection, application_id=item["application_id"],
                                             kinds=("bid",), actor_id=actor["actor_id"],
                                             note="项目换版，在审申请失效，释放投标担保")
                self._audit(connection, actor_id=actor_id, action="project.version_published",
                            resource_type="project", resource_id=project_id,
                            detail={"version_no": version_no, "invalidated": len(pending),
                                    "rule_version_id": rule["version_id"]})
                return {"resource_type": "project_version", "resource_id": f"{project_id}:{version_no}",
                        "project_id": project_id, "version_no": version_no,
                        "invalidated_applications": len(pending)}

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_project_version", payload=payload, create=create)

    def _app_next_seq(self, connection, application_id: str) -> int:
        return connection.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS next FROM application_events WHERE application_id=?",
            (application_id,)).fetchone()["next"]

    # ------------------------------------------------------------------
    # 竞争申请：同一关联组 / 同一资质件不得在同一项目重复竞争
    # ------------------------------------------------------------------

    def submit_application(self, *, request_id: str, actor_id: str, application_id: str,
                           project_id: str, lead_investor_id: str,
                           members: list[dict[str, Any]],
                           contributions: dict[str, float],
                           evidence_ids: list[str],
                           assets: list[dict[str, Any]] | None = None,
                           note: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "application_id": application_id, "project_id": project_id,
                   "lead_investor_id": lead_investor_id, "members": members,
                   "contributions": contributions, "evidence_ids": evidence_ids, "assets": assets or []}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator")
            replay = self._replay(connection, request_id=request_id,
                                  action="submit_application", payload=payload)
            if replay is not None:
                return replay
            project = connection.execute("SELECT * FROM projects WHERE project_id=?",
                                         (project_id,)).fetchone()
            if project is None:
                raise NotFoundError("项目不存在")
            if project["status"] != "open":
                raise ConflictError("项目已经授标或关闭，不再接受申请")
            rule_row, rules = self._load_rules(connection, self._current_rule(connection)["version_id"])
            members = self._validate_members(connection, members, lead_investor_id,
                                             contributions, rules)
            evidence_rows = self._validate_application_evidence(connection, evidence_ids, members)
            assets = assets or []
            self._validate_assets(connection, assets, members)
            self._check_no_duplicate_bidding(connection, project_id=project_id,
                                             members=members, evidence_rows=evidence_rows)

            def create():
                app_seq = self._nextval(connection, "application")
                plan = {"contributions": {m["investor_id"]: float(contributions[m["investor_id"]])
                                          for m in members},
                        "assets": [{"asset_id": a["asset_id"],
                                    "amount": float(a["amount"])} for a in assets]}
                connection.execute(
                    "INSERT INTO applications(application_id,project_id,project_version,"
                    "rule_version_id,lead_investor_id,plan_json,payload_hash,app_seq,status,"
                    "submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,'submitted',?,?)",
                    (application_id, project_id, project["current_version"], rule_row["version_id"],
                     lead_investor_id, canonical_json(plan), digest(plan), app_seq,
                     actor["actor_id"], self._now()))
                for member in members:
                    investor = connection.execute(
                        "SELECT kind FROM investors WHERE investor_id=?",
                        (member["investor_id"],)).fetchone()
                    is_private = 1 if member.get("is_private", investor["kind"] == "private") else 0
                    connection.execute(
                        "INSERT INTO application_members(application_id,investor_id,member_role,"
                        "share,entry_phase,is_private) VALUES(?,?,?,?,?,?)",
                        (application_id, member["investor_id"], member["member_role"],
                         float(member["share"]), int(member["entry_phase"]), is_private))
                for ev in evidence_rows:
                    connection.execute(
                        "INSERT INTO application_evidence(application_id,evidence_id,investor_id,"
                        "document_hash) VALUES(?,?,?,?)",
                        (application_id, ev["evidence_id"], ev["investor_id"], ev["document_hash"]))
                # 申请即预占承诺资产；落选/撤回/失效时释放，唯一索引兜底跨项目重复承诺
                for item in assets:
                    try:
                        connection.execute(
                            "INSERT INTO asset_engagements(engagement_id,asset_id,project_id,"
                            "application_id,amount,engaged_at) VALUES(?,?,?,?,?,?)",
                            (uuid.uuid4().hex, item["asset_id"], project_id,
                             application_id, float(item["amount"]), self._now()))
                    except Exception as exc:
                        raise ConflictError(f"资产 {item['asset_id']} 已被其他项目占用") from exc
                connection.execute(
                    "INSERT INTO application_events(application_id,seq,status,actor_id,note,at) "
                    "VALUES(?,1,'submitted',?,?,?)",
                    (application_id, actor["actor_id"], note, self._now()))
                self._audit(connection, actor_id=actor_id, action="application.submitted",
                            resource_type="application", resource_id=application_id,
                            detail={"project_id": project_id, "members": len(members),
                                    "rule_version_id": rule_row["version_id"],
                                    "project_version": project["current_version"],
                                    "app_seq": app_seq})
                return {"resource_type": "application", "resource_id": application_id,
                        "application_id": application_id, "app_seq": app_seq,
                        "rule_version_id": rule_row["version_id"],
                        "project_version": project["current_version"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_application", payload=payload, create=create)

    def _validate_members(self, connection, members, lead_id, contributions, rules) -> list[dict]:
        if not isinstance(members, list) or not members:
            raise ValidationError("members 必须是非空数组")
        normalized: list[dict[str, Any]] = []
        total_share = 0.0
        private_share = 0.0
        leads = 0
        seen = set()
        investor_ids = set()
        for raw in members:
            investor_id = raw.get("investor_id")
            share = raw.get("share")
            role = raw.get("member_role", "member")
            phase = raw.get("entry_phase", 1)
            if not isinstance(share, (int, float)) or not 0 <= share <= 1:
                raise ValidationError("成员 share 必须是 0 到 1 的数字")
            if role not in ("lead", "member"):
                raise ValidationError("member_role 只能是 lead 或 member")
            if not isinstance(phase, int) or phase < 1:
                raise ValidationError("entry_phase 必须是不小于 1 的整数")
            if investor_id in seen:
                raise ValidationError(f"成员 {investor_id} 重复出现")
            seen.add(investor_id)
            investor_ids.add(investor_id)
            if connection.execute("SELECT 1 FROM investors WHERE investor_id=?",
                                  (investor_id,)).fetchone() is None:
                raise NotFoundError(f"投资人 {investor_id} 不存在")
            if role == "lead":
                leads += 1
                if investor_id != lead_id:
                    raise ValidationError("牵头人必须是成员中的 lead")
                if int(phase) != 1:
                    raise ValidationError("牵头人必须在首期入场")
            row = connection.execute("SELECT kind FROM investors WHERE investor_id=?",
                                     (investor_id,)).fetchone()
            is_private = bool(raw.get("is_private", row["kind"] == "private"))
            if is_private:
                private_share += float(share)
            total_share += float(share)
            normalized.append({"investor_id": investor_id, "member_role": role,
                               "share": float(share), "entry_phase": phase,
                               "is_private": is_private})
        if leads != 1:
            raise ValidationError("联合体必须恰好有一名 lead")
        if lead_id not in investor_ids:
            raise ValidationError("牵头人必须在成员名单内")
        if abs(total_share - 1.0) > 1e-9:
            raise ValidationError(f"成员股比之和必须为 1，当前为 {total_share}")
        if private_share + 1e-9 < float(rules["min_private_ratio"]):
            raise ValidationError(
                f"民营股比 {private_share:.4f} 低于规则门槛 {rules['min_private_ratio']}")
        if not isinstance(contributions, dict) or set(contributions) != investor_ids:
            raise ValidationError("contributions 必须为每个成员填写出资额")
        for value in contributions.values():
            if not isinstance(value, (int, float)) or value <= 0:
                raise ValidationError("出资额必须为正数")
        return normalized

    def _validate_application_evidence(self, connection, evidence_ids, members):
        if not isinstance(evidence_ids, list) or not evidence_ids:
            raise ValidationError("evidence_ids 必须是非空数组")
        member_ids = {m["investor_id"] for m in members}
        rows = []
        seen = set()
        kinds_present = set()
        for evidence_id in evidence_ids:
            if evidence_id in seen:
                raise ValidationError(f"证据 {evidence_id} 重复提交")
            seen.add(evidence_id)
            row, status, valid = self._evidence_state(connection, evidence_id)
            if row["investor_id"] not in member_ids:
                raise ValidationError(f"证据 {evidence_id} 不属于联合体成员")
            if not valid:
                raise ConflictError(f"证据 {evidence_id} 未通过核验、已吊销或已过期")
            kinds_present.add(row["evidence_kind"])
            rows.append(row)
        return rows

    def _validate_assets(self, connection, assets, members) -> None:
        member_ids = {m["investor_id"] for m in members}
        seen = set()
        for item in assets:
            asset_id = item.get("asset_id")
            amount = item.get("amount", 0)
            if not isinstance(amount, (int, float)) or amount < 0:
                raise ValidationError("资产承诺金额必须是非负数")
            if asset_id in seen:
                raise ValidationError(f"资产 {asset_id} 在同一申请中重复出现")
            seen.add(asset_id)
            row = connection.execute("SELECT * FROM pledged_assets WHERE asset_id=?",
                                     (asset_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"资产 {asset_id} 不存在")
            if row["investor_id"] not in member_ids:
                raise ValidationError(f"资产 {asset_id} 不属于联合体成员")
            active = connection.execute(
                "SELECT project_id FROM asset_engagements WHERE asset_id=? AND released_at IS NULL",
                (asset_id,)).fetchone()
            if active is not None:
                raise ConflictError(f"资产 {asset_id} 已经被项目 {active['project_id']} 占用，不得重复承诺")

    def _validate_change_assets(self, connection, *, body, investor_id) -> None:
        for item in body.get("assets", []):
            asset_id = item.get("asset_id")
            amount = item.get("amount", 0)
            if not isinstance(amount, (int, float)) or amount < 0:
                raise ValidationError("资产承诺金额必须是非负数")
            row = connection.execute("SELECT * FROM pledged_assets WHERE asset_id=?",
                                     (asset_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"资产 {asset_id} 不存在")
            if row["investor_id"] != investor_id:
                raise ValidationError(f"资产 {asset_id} 不属于变动成员 {investor_id}")
            active = connection.execute(
                "SELECT project_id FROM asset_engagements WHERE asset_id=? AND released_at IS NULL",
                (asset_id,)).fetchone()
            if active is not None:
                raise ConflictError(f"资产 {asset_id} 已经被项目 {active['project_id']} 占用")

    def _check_no_duplicate_bidding(self, connection, *, project_id, members, evidence_rows) -> None:
        groups = self._related_groups(connection)
        member_ids = {m["investor_id"] for m in members}
        new_groups = {groups[i] for i in member_ids if i in groups}
        doc_hashes = {row["document_hash"] for row in evidence_rows}
        competing = connection.execute(
            "SELECT application_id FROM applications WHERE project_id=? AND status='submitted'",
            (project_id,)).fetchall()
        for app in competing:
            other_members = connection.execute(
                "SELECT investor_id FROM application_members WHERE application_id=?",
                (app["application_id"],)).fetchall()
            for other in other_members:
                other_id = other["investor_id"]
                if other_id in member_ids:
                    raise ConflictError(
                        f"主体 {other_id} 已在申请 {app['application_id']} 中参与同一项目")
                if groups.get(other_id) in new_groups:
                    raise ConflictError(
                        f"关联主体组 {groups[other_id]} 已通过申请 {app['application_id']} "
                        "参与同一项目，禁止重复竞争")
            other_docs = connection.execute(
                "SELECT document_hash FROM application_evidence WHERE application_id=?",
                (app["application_id"],)).fetchall()
            overlap = {r["document_hash"] for r in other_docs} & doc_hashes
            if overlap:
                raise ConflictError(
                    f"资质文件 {sorted(overlap)[0]} 已被申请 {app['application_id']} 使用，禁止重复提交")

    # ------------------------------------------------------------------
    # 担保：投标担保与履约担保
    # ------------------------------------------------------------------

    def post_guarantee(self, *, request_id: str, actor_id: str, guarantee_id: str,
                       application_id: str, investor_id: str, guarantee_kind: str,
                       amount: float) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "guarantee_id": guarantee_id, "application_id": application_id,
                   "investor_id": investor_id, "guarantee_kind": guarantee_kind, "amount": amount}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator")
            if guarantee_kind not in ("bid", "performance"):
                raise ValidationError("guarantee_kind 只能是 bid 或 performance")
            if not isinstance(amount, (int, float)) or amount <= 0:
                raise ValidationError("担保金额必须为正数")
            app = connection.execute("SELECT * FROM applications WHERE application_id=?",
                                     (application_id,)).fetchone()
            if app is None:
                raise NotFoundError("申请不存在")
            membership = connection.execute(
                "SELECT 1 FROM application_members WHERE application_id=? AND investor_id=?",
                (application_id, investor_id)).fetchone()
            if membership is None:
                # 允许为"新增成员会签在途"的投资人预缴投标担保
                pending = connection.execute(
                    "SELECT 1 FROM review_tasks t JOIN agreements a ON a.agreement_id=t.agreement_id "
                    "WHERE a.application_id=? AND t.status='pending' AND t.kind='member_add' "
                    "AND json_extract(t.payload_json,'$.investor_id')=?",
                    (application_id, investor_id)).fetchone()
                if pending is None:
                    raise ValidationError("该投资人不是此申请的联合体成员，且无在途新增会签")
                if guarantee_kind != "bid":
                    raise ConflictError("新成员在会签通过前只能缴纳投标担保")
            if guarantee_kind == "performance" and app["status"] != "selected":
                raise ConflictError("履约担保只能在授标之后缴纳")

            def create():
                connection.execute(
                    "INSERT INTO guarantees(guarantee_id,project_id,application_id,investor_id,"
                    "guarantee_kind,amount,status,posted_by,posted_at) VALUES(?,?,?,?,?,?,'held',?,?)",
                    (guarantee_id, app["project_id"], application_id, investor_id,
                     guarantee_kind, float(amount), actor_id, self._now()))
                connection.execute(
                    "INSERT INTO guarantee_events(guarantee_id,seq,action,amount,actor_id,at,note) "
                    "VALUES(?,1,'post',?,?,?,'')",
                    (guarantee_id, float(amount), actor_id, self._now()))
                self._audit(connection, actor_id=actor_id, action="guarantee.posted",
                            resource_type="guarantee", resource_id=guarantee_id,
                            detail={"application_id": application_id, "investor_id": investor_id,
                                    "kind": guarantee_kind, "amount": float(amount)})
                return {"resource_type": "guarantee", "resource_id": guarantee_id,
                        "guarantee_id": guarantee_id, "status": "held"}

            return self._idempotent(connection, request_id=request_id,
                                    action="post_guarantee", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 会签队列：全局 FIFO，持久化，重启后顺序不变
    # ------------------------------------------------------------------

    def submit_review_task(self, *, request_id: str, actor_id: str, kind: str,
                           payload: dict[str, Any]) -> dict[str, Any]:
        if kind not in TASK_KIND_ROLES:
            raise ValidationError("kind 不在允许的会签事项范围内")
        if not isinstance(payload, dict) or not payload:
            raise ValidationError("payload 必须是非空对象")
        payload = {"actor_id": actor_id, "kind": kind, "payload": payload}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator")
            replay = self._replay(connection, request_id=request_id,
                                  action="submit_review_task", payload=payload)
            if replay is not None:
                return replay
            self._validate_task_payload(connection, kind, payload["payload"])

            def create():
                task_seq = self._nextval(connection, "review_task")
                task_id = f"task-{task_seq:05d}"
                body = payload["payload"]
                required = list(TASK_KIND_ROLES[kind])
                application_id = body.get("application_id") if kind == "admission" else None
                agreement_id = body.get("agreement_id") if kind != "admission" else None
                connection.execute(
                    "INSERT INTO review_tasks(task_id,task_seq,project_id,application_id,agreement_id,"
                    "kind,payload_json,payload_hash,required_roles_json,submitter,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?)",
                    (task_id, task_seq, body["project_id"], application_id, agreement_id,
                     kind, canonical_json(body), digest(body), canonical_json(required),
                     actor_id, self._now()))
                # 成员变动涉及的资产在提单时即预占，会签驳回时按 task_id 释放
                if kind in MEMBER_CHANGE_KINDS:
                    app_ref = connection.execute(
                        "SELECT application_id FROM agreements WHERE agreement_id=?",
                        (body["agreement_id"],)).fetchone()["application_id"]
                    for item in body.get("assets", []):
                        try:
                            connection.execute(
                                "INSERT INTO asset_engagements(engagement_id,asset_id,project_id,"
                                "application_id,task_id,amount,engaged_at) VALUES(?,?,?,?,?,?,?)",
                                (uuid.uuid4().hex, item["asset_id"], body["project_id"],
                                 app_ref, task_id, float(item.get("amount", 0)), self._now()))
                        except Exception as exc:
                            raise ConflictError(
                                f"资产 {item['asset_id']} 已被其他项目占用") from exc
                self._audit(connection, actor_id=actor_id, action="review.submitted",
                            resource_type="review_task", resource_id=task_id,
                            detail={"kind": kind, "seq": task_seq, "required_roles": required})
                return {"resource_type": "review_task", "resource_id": task_id,
                        "task_id": task_id, "task_seq": task_seq,
                        "required_roles": required, "status": "pending"}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_review_task", payload=payload, create=create)

    def _validate_task_payload(self, connection, kind: str, body: dict[str, Any]) -> None:
        if kind == "admission":
            app = connection.execute("SELECT * FROM applications WHERE application_id=?",
                                     (body.get("application_id"),)).fetchone()
            if app is None:
                raise NotFoundError("申请不存在")
            if app["status"] != "submitted":
                raise ConflictError("只有在审申请可以提交准入会签")
            body["project_id"] = app["project_id"]
            return
        agreement = connection.execute("SELECT * FROM agreements WHERE agreement_id=?",
                                       (body.get("agreement_id"),)).fetchone()
        if agreement is None:
            raise NotFoundError("协议不存在")
        if agreement["status"] != "active" and kind != "default_exit":
            raise ConflictError("协议已终止")
        body["project_id"] = agreement["project_id"]
        investor_id = body.get("investor_id")
        if kind in MEMBER_CHANGE_KINDS:
            if not investor_id:
                raise ValidationError("成员变动必须指定 investor_id")
            exists = connection.execute(
                "SELECT 1 FROM investors WHERE investor_id=?", (investor_id,)).fetchone()
            if not exists:
                raise NotFoundError("投资人不存在")
            share = body.get("share")
            if not isinstance(share, (int, float)) or not 0 < share <= 1:
                raise ValidationError("新成员 share 必须是大于 0 且不超过 1 的数字")
            app_member = connection.execute(
                "SELECT entry_phase FROM application_members WHERE application_id=? AND investor_id=?",
                (agreement["application_id"], investor_id)).fetchone()
            current = self._current_members(connection, agreement["agreement_id"])
            if kind == "phased_entry":
                if app_member is None:
                    raise ValidationError("分期入场对象必须是原申请联合体中预留的后续期次成员")
                if investor_id in current:
                    raise ConflictError("该投资人已经入场")
                # 股比必须与申请时承诺一致，防止借分期入场调整股比
                committed = connection.execute(
                    "SELECT share FROM application_members WHERE application_id=? AND investor_id=?",
                    (agreement["application_id"], investor_id)).fetchone()["share"]
                if abs(float(share) - float(committed)) > 1e-9:
                    raise ValidationError("分期入场股比必须与申请承诺一致")
            else:  # member_add：全新主体加入，存量成员按 new_shares 稀释，重验民营比例
                if app_member is not None or investor_id in current:
                    raise ConflictError("该投资人已经在联合体中")
                new_shares = body.get("new_shares")
                if not isinstance(new_shares, dict):
                    raise ValidationError("member_add 必须提供变更后完整股比表 new_shares")
                expected = set(current) | {investor_id}
                if set(new_shares) != expected:
                    raise ValidationError(
                        f"new_shares 必须且只能包含变更后全体成员：{sorted(expected)}")
                total = 0.0
                for member_id, value in new_shares.items():
                    if not isinstance(value, (int, float)) or not 0 <= value <= 1:
                        raise ValidationError("股比必须是 0 到 1 的数字")
                    total += float(value)
                    if member_id in current and float(value) > float(current[member_id]["share"]) + 1e-9:
                        raise ConflictError("新增成员只能稀释存量成员股比，不得调高任何存量成员股比")
                if float(new_shares[investor_id]) <= 0:
                    raise ValidationError("新成员股比必须大于 0")
                if abs(total - 1.0) > 1e-9:
                    raise ValidationError(f"变更后股比之和必须为 1，当前为 {total}")
                _, rules = self._load_rules(connection, agreement["rule_version_id"])
                member_rows = connection.execute(
                    "SELECT investor_id,share,is_private FROM application_members "
                    "WHERE application_id=?", (agreement["application_id"],)).fetchall()
                private_by_id = {r["investor_id"]: bool(r["is_private"]) for r in member_rows}
                investor = connection.execute("SELECT kind FROM investors WHERE investor_id=?",
                                              (investor_id,)).fetchone()
                private_by_id[investor_id] = bool(
                    body.get("is_private", investor["kind"] == "private"))
                private_sum = sum(float(share) for member_id, share in new_shares.items()
                                  if private_by_id.get(member_id))
                if private_sum + 1e-9 < float(rules["min_private_ratio"]):
                    raise ValidationError(
                        f"新增成员后民营股比 {private_sum:.4f} 低于规则门槛 {rules['min_private_ratio']}")
            self._validate_change_assets(connection, body=body, investor_id=investor_id)
        elif kind == "member_remove":
            if not investor_id:
                raise ValidationError("退出成员必须指定 investor_id")
            if not self._current_members(connection, agreement["agreement_id"]).get(investor_id):
                raise NotFoundError("该投资人当前不是联合体成员")
            if investor_id == agreement["controller_id"]:
                raise ConflictError("控股方不能以普通成员退出方式离开，请先办理控制权变更")
        elif kind == "control_change":
            new_controller = body.get("new_controller_id")
            if not new_controller:
                raise ValidationError("控制权变更必须指定 new_controller_id")
            if new_controller == agreement["controller_id"]:
                raise ConflictError("新控股方与当前控股方相同")
            if not self._current_members(connection, agreement["agreement_id"]).get(new_controller):
                raise ValidationError("新控股方必须是当前联合体成员")

    def list_review_queue(self, status: str = "pending") -> list[dict[str, Any]]:
        import json
        if status not in ("pending", "approved", "rejected"):
            raise ValidationError("status 不合法")
        rows = self.database.connection.execute(
            "SELECT * FROM review_tasks WHERE status=? ORDER BY task_seq", (status,)).fetchall()
        items = []
        for row in rows:
            signatures = self.database.connection.execute(
                "SELECT role,actor_id,decision,note,signed_at FROM review_signatures "
                "WHERE task_id=? ORDER BY role", (row["task_id"],)).fetchall()
            items.append({"task_id": row["task_id"], "task_seq": row["task_seq"],
                          "project_id": row["project_id"], "kind": row["kind"],
                          "application_id": row["application_id"],
                          "agreement_id": row["agreement_id"],
                          "required_roles": json.loads(row["required_roles_json"]),
                          "submitter": row["submitter"], "status": row["status"],
                          "created_at": row["created_at"],
                          "signatures": [dict(s) for s in signatures]})
        return items

    def sign_review_task(self, *, request_id: str, actor_id: str, task_id: str,
                         decision: str, note: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "task_id": task_id, "decision": decision, "note": note}
        if decision not in ("approved", "rejected"):
            raise ValidationError("decision 只能是 approved 或 rejected")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            replay = self._replay(connection, request_id=request_id,
                                  action="sign_review_task", payload=payload)
            if replay is not None:
                return replay
            import json
            task = connection.execute("SELECT * FROM review_tasks WHERE task_id=?",
                                      (task_id,)).fetchone()
            if task is None:
                raise NotFoundError("会签事项不存在")
            if task["status"] != "pending":
                raise ConflictError("会签事项已经办结")
            required = json.loads(task["required_roles_json"])
            self._require_roles(actor, *required)
            if actor["actor_id"] == task["submitter"]:
                raise PermissionDenied("职责分离：提单人不能参与本事项会签")
            prior = connection.execute(
                "SELECT role,actor_id FROM review_signatures WHERE task_id=?", (task_id,)).fetchall()
            if any(p["actor_id"] == actor["actor_id"] for p in prior):
                raise PermissionDenied("同一操作者不能重复会签")
            if actor["role"] not in required or any(p["role"] == actor["role"] for p in prior):
                raise ConflictError("当前角色的会签槽位不存在或已签署")
            # 严格 FIFO：未办结审查按提交顺序处理，任何角色都不能先签排在前面的事项
            self._enforce_queue_head(connection, task)

            def create():
                connection.execute(
                    "INSERT INTO review_signatures(task_id,role,actor_id,decision,note,signed_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (task_id, actor["role"], actor_id, decision, note, self._now()))
                self._audit(connection, actor_id=actor_id,
                            action=f"review.{decision}", resource_type="review_task",
                            resource_id=task_id, detail={"kind": task["kind"], "role": actor["role"]})
                finished = False
                if decision == "rejected":
                    connection.execute("UPDATE review_tasks SET status='rejected' WHERE task_id=?",
                                       (task_id,))
                    # 驳回即释放该事项预占的资产
                    connection.execute(
                        "UPDATE asset_engagements SET released_at=? WHERE task_id=? "
                        "AND released_at IS NULL", (self._now(), task_id))
                    # 新增成员未通过会签：释放其预缴投标担保
                    if task["kind"] == "member_add":
                        body = json.loads(task["payload_json"])
                        application_id = connection.execute(
                            "SELECT application_id FROM agreements WHERE agreement_id=?",
                            (task["agreement_id"],)).fetchone()["application_id"]
                        self._release_investor_guarantees(
                            connection, application_id=application_id,
                            investor_id=body["investor_id"], actor_id=actor_id,
                            note="新增成员会签驳回，释放预缴投标担保")
                    self._audit(connection, actor_id=actor_id, action="review.closed",
                                resource_type="review_task", resource_id=task_id,
                                detail={"result": "rejected", "kind": task["kind"]})
                    finished = True
                else:
                    signed_roles = {p["role"] for p in prior} | {actor["role"]}
                    if set(required) <= signed_roles:
                        self._finalize_task(connection, task, actor_id)
                        finished = True
                return {"resource_type": "review_task", "resource_id": task_id,
                        "task_id": task_id, "status": self._task_status(connection, task_id),
                        "finished": finished}

            return self._idempotent(connection, request_id=request_id,
                                    action="sign_review_task", payload=payload, create=create)

    def _enforce_queue_head(self, connection, task) -> None:
        head = connection.execute(
            "SELECT task_id FROM review_tasks WHERE status='pending' ORDER BY task_seq LIMIT 1"
        ).fetchone()
        if head is None or head["task_id"] != task["task_id"]:
            raise ConflictError("会签队列按提交顺序办结，请先处理排在前面的事项")

    def _task_status(self, connection, task_id: str) -> str:
        return connection.execute("SELECT status FROM review_tasks WHERE task_id=?",
                                  (task_id,)).fetchone()["status"]

    # ------------------------------------------------------------------
    # 会签通过后的生效动作
    # ------------------------------------------------------------------

    def _finalize_task(self, connection, task, actor_id: str) -> None:
        import json
        body = json.loads(task["payload_json"])
        if task["kind"] == "admission":
            self._admit(connection, body, task, actor_id)
        elif task["kind"] in MEMBER_CHANGE_KINDS:
            self._apply_member_change(connection, body, task, actor_id)
        elif task["kind"] == "member_remove":
            self._apply_member_remove(connection, body, task, actor_id)
        elif task["kind"] == "control_change":
            self._apply_control_change(connection, body, task, actor_id)
        elif task["kind"] == "default_exit":
            self._apply_default_exit(connection, body, task, actor_id)
        connection.execute("UPDATE review_tasks SET status='approved' WHERE task_id=?",
                           (task["task_id"],))
        self._audit(connection, actor_id=actor_id, action="review.closed",
                    resource_type="review_task", resource_id=task["task_id"],
                    detail={"result": "approved", "kind": task["kind"]})

    def _admit(self, connection, body, task, actor_id: str) -> None:
        app = connection.execute("SELECT * FROM applications WHERE application_id=?",
                                 (body["application_id"],)).fetchone()
        if app["status"] != "submitted":
            raise ConflictError("申请状态已经变化，不能授标")
        project = connection.execute("SELECT * FROM projects WHERE project_id=?",
                                     (app["project_id"],)).fetchone()
        if app["project_version"] != project["current_version"]:
            raise ConflictError("项目已发布新版本，该申请需重新提交")
        selected = connection.execute(
            "SELECT 1 FROM applications WHERE project_id=? AND status='selected'",
            (app["project_id"],)).fetchone()
        if selected:
            raise ConflictError("该项目已有生效的成交结果")
        _, rules = self._load_rules(connection, app["rule_version_id"])
        # 授标前再次确认证据有效、投标担保足额
        self._recheck_evidence(connection, app["application_id"])
        self._check_bid_guarantees(connection, app, rules)

        agreement_id = f"agr-{app['application_id']}"
        plan_json = app["plan_json"]
        now = self._now()
        connection.execute(
            "INSERT INTO agreements(agreement_id,project_id,application_id,rule_version_id,"
            "lead_investor_id,controller_id,plan_version,status,signed_at) "
            "VALUES(?,?,?,?,?,?,1,'active',?)",
            (agreement_id, app["project_id"], app["application_id"], app["rule_version_id"],
             app["lead_investor_id"], app["lead_investor_id"], now))
        connection.execute(
            "INSERT INTO agreement_plans(agreement_id,version_no,plan_json,payload_hash,"
            "created_by_task,created_at) VALUES(?,1,?,?,?,?)",
            (agreement_id, plan_json, app["payload_hash"], task["task_id"], now))
        seq = 0
        members = connection.execute(
            "SELECT * FROM application_members WHERE application_id=? ORDER BY investor_id",
            (app["application_id"],)).fetchall()
        admitted = []
        for member in members:
            if member["entry_phase"] != 1:
                continue  # 后续期次成员通过 phased_entry 会签入场
            admitted.append(member)
            seq += 1
            connection.execute(
                "INSERT INTO membership_events(event_id,agreement_id,seq,kind,investor_id,"
                "payload_json,actor_id,at) VALUES(?,?,?, 'admitted', ?,?,?,?)",
                (uuid.uuid4().hex, agreement_id, seq, member["investor_id"],
                 canonical_json({"share": member["share"], "entry_phase": member["entry_phase"]}),
                 actor_id, now))
        connection.execute(
            "INSERT INTO agreement_events(agreement_id,seq,status,actor_id,note,at) "
            "VALUES(?,1,'active',?,'准入会签通过',?)",
            (agreement_id, actor_id, now))
        # 资产已在申请提交时预占，授标时确认归属，无需重复占用
        # 唯一成交：本申请中标，其余在审申请淘汰，其投标担保可释放
        others = connection.execute(
            "SELECT application_id FROM applications WHERE project_id=? AND status='submitted' "
            "AND application_id<>?",
            (app["project_id"], app["application_id"])).fetchall()
        connection.execute("UPDATE applications SET status='selected' WHERE application_id=?",
                           (app["application_id"],))
        connection.execute(
            "INSERT INTO application_events(application_id,seq,status,actor_id,note,at) "
            "VALUES(?,?,'selected',?,'准入会签通过，唯一成交',?)",
            (app["application_id"], self._app_next_seq(connection, app["application_id"]),
             actor_id, now))
        for other in others:
            connection.execute("UPDATE applications SET status='rejected' WHERE application_id=?",
                               (other["application_id"],))
            connection.execute(
                "INSERT INTO application_events(application_id,seq,status,actor_id,note,at) "
                "VALUES(?,?,'rejected',?,'竞争申请唯一成交，本申请未中标',?)",
                (other["application_id"], self._app_next_seq(connection, other["application_id"]),
                 actor_id, now))
            self._release_guarantees(connection, application_id=other["application_id"],
                                     kinds=("bid",), actor_id=actor_id, note="未中标释放投标担保")
            # 落选申请的承诺资产同步解除占用，可投向其他项目
            connection.execute(
                "UPDATE asset_engagements SET released_at=? WHERE application_id=? "
                "AND released_at IS NULL", (now, other["application_id"]))
        connection.execute("UPDATE projects SET status='awarded' WHERE project_id=?",
                           (app["project_id"],))
        self._audit(connection, actor_id=actor_id, action="agreement.signed",
                    resource_type="agreement", resource_id=agreement_id,
                    detail={"project_id": app["project_id"],
                            "application_id": app["application_id"],
                            "rule_version_id": app["rule_version_id"],
                            "members_admitted": len(admitted),
                            "members_total": len(members)})

    def _recheck_evidence(self, connection, application_id: str) -> None:
        rows = connection.execute(
            "SELECT evidence_id FROM application_evidence WHERE application_id=?",
            (application_id,)).fetchall()
        for row in rows:
            _, _, valid = self._evidence_state(connection, row["evidence_id"])
            if not valid:
                raise ConflictError(f"证据 {row['evidence_id']} 已失效，不能授标")

    def _check_bid_guarantees(self, connection, app, rules) -> None:
        rate = float(rules.get("guarantee_rates", {}).get("bid", 0))
        plan = json_loads(app["plan_json"])
        first_phase = {
            row["investor_id"] for row in connection.execute(
                "SELECT investor_id FROM application_members WHERE application_id=? AND entry_phase=1",
                (app["application_id"],)).fetchall()
        }
        for investor_id, contribution in plan["contributions"].items():
            if investor_id not in first_phase:
                continue  # 后续期次成员在其入场会签时校验
            row = connection.execute(
                "SELECT COALESCE(SUM(amount),0) AS total FROM guarantees "
                "WHERE application_id=? AND investor_id=? AND guarantee_kind='bid' "
                "AND status IN ('held','partial')",
                (app["application_id"], investor_id)).fetchone()
            needed = float(contribution) * rate
            if row["total"] + 1e-9 < needed:
                raise ConflictError(
                    f"投资人 {investor_id} 投标担保不足：需 {needed:.2f}，实缴 {row['total']:.2f}")

    def _release_investor_guarantees(self, connection, *, application_id, investor_id,
                                     kinds=("bid",), actor_id=None, note="") -> float:
        """释放某投资人在某申请下指定类型担保的可用余额，返回释放总额。"""

        total = 0.0
        rows = connection.execute(
            "SELECT guarantee_id FROM guarantees WHERE application_id=? AND investor_id=? "
            "AND guarantee_kind IN (%s) AND status IN ('held','partial')"
            % ",".join("?" for _ in kinds),
            (application_id, investor_id, *kinds)).fetchall()
        for row in rows:
            available = self._guarantee_available(connection, row["guarantee_id"])
            if available <= 1e-9:
                continue
            seq = connection.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS next FROM guarantee_events WHERE guarantee_id=?",
                (row["guarantee_id"],)).fetchone()["next"]
            connection.execute(
                "INSERT INTO guarantee_events(guarantee_id,seq,action,amount,actor_id,at,note) "
                "VALUES(?,?,'release',?,?,?,?)",
                (row["guarantee_id"], seq, available, actor_id, self._now(), note))
            connection.execute("UPDATE guarantees SET status='released' WHERE guarantee_id=?",
                               (row["guarantee_id"],))
            total += available
        return total

    def _release_guarantees(self, connection, *, application_id, kinds, actor_id, note=""):
        rows = connection.execute(
            "SELECT * FROM guarantees WHERE application_id=? AND guarantee_kind IN (%s) "
            "AND status IN ('held','partial')" % ",".join("?" for _ in kinds),
            (application_id, *kinds)).fetchall()
        for row in rows:
            seq = connection.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS next FROM guarantee_events WHERE guarantee_id=?",
                (row["guarantee_id"],)).fetchone()["next"]
            connection.execute(
                "INSERT INTO guarantee_events(guarantee_id,seq,action,amount,actor_id,at,note) "
                "VALUES(?,?,'release',?,?,?,?)",
                (row["guarantee_id"], seq, row["amount"], actor_id, self._now(), note))
            connection.execute("UPDATE guarantees SET status='released' WHERE guarantee_id=?",
                               (row["guarantee_id"],))

    def _current_members(self, connection, agreement_id: str) -> dict[str, dict]:
        """返回当前在场成员；股比以申请成员表的最新值（含稀释）为准。"""

        application_id = connection.execute(
            "SELECT application_id FROM agreements WHERE agreement_id=?",
            (agreement_id,)).fetchone()["application_id"]
        present: set[str] = set()
        events = connection.execute(
            "SELECT kind,investor_id,payload_json FROM membership_events WHERE agreement_id=? "
            "ORDER BY seq", (agreement_id,)).fetchall()
        for row in events:
            if row["kind"] in ("admitted", "member_added", "phase_entered"):
                present.add(row["investor_id"])
            elif row["kind"] in ("member_removed", "exited"):
                present.discard(row["investor_id"])
        members: dict[str, dict] = {}
        if present:
            placeholders = ",".join("?" for _ in present)
            rows = connection.execute(
                f"SELECT investor_id,share,entry_phase FROM application_members "
                f"WHERE application_id=? AND investor_id IN ({placeholders})",
                (application_id, *present)).fetchall()
            for row in rows:
                members[row["investor_id"]] = {"share": float(row["share"]),
                                               "entry_phase": int(row["entry_phase"])}
        return members

    def _membership_next_seq(self, connection, agreement_id: str) -> int:
        return connection.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS next FROM membership_events WHERE agreement_id=?",
            (agreement_id,)).fetchone()["next"]

    def _apply_member_change(self, connection, body, task, actor_id: str) -> None:
        agreement_id = body["agreement_id"]
        investor_id = body["investor_id"]
        now = self._now()
        kind = "phase_entered" if task["kind"] == "phased_entry" else "member_added"
        seq = self._membership_next_seq(connection, agreement_id)
        entry_phase = int(body.get("entry_phase", 1))
        payload = {"share": float(body["share"]), "entry_phase": entry_phase}
        connection.execute(
            "INSERT INTO membership_events(event_id,agreement_id,seq,kind,investor_id,"
            "payload_json,actor_id,at) VALUES(?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, agreement_id, seq, kind, investor_id,
             canonical_json(payload), actor_id, now))
        if task["kind"] == "member_add":
            investor = connection.execute("SELECT kind FROM investors WHERE investor_id=?",
                                          (investor_id,)).fetchone()
            is_private = 1 if body.get("is_private", investor["kind"] == "private") else 0
            application_id = self._application_of(connection, agreement_id)
            connection.execute(
                "INSERT INTO application_members(application_id,investor_id,member_role,"
                "share,entry_phase,is_private) VALUES(?,?,'member',?,?,?)",
                (application_id, investor_id, float(body["share"]), entry_phase, is_private))
            # 按会签确认的变更后股比表稀释存量成员
            for member_id, new_share in body["new_shares"].items():
                connection.execute(
                    "UPDATE application_members SET share=? WHERE application_id=? AND investor_id=?",
                    (float(new_share), application_id, member_id))
        # 入场成员必须按协议锁定的规则版本缴足投标担保
        agreement = connection.execute("SELECT * FROM agreements WHERE agreement_id=?",
                                       (agreement_id,)).fetchone()
        _, pinned_rules = self._load_rules(connection, agreement["rule_version_id"])
        application_id = self._application_of(connection, agreement_id)
        latest_plan = connection.execute(
            "SELECT plan_json FROM agreement_plans WHERE agreement_id=? ORDER BY version_no DESC LIMIT 1",
            (agreement_id,)).fetchone()
        plan = json_loads(latest_plan["plan_json"])
        if task["kind"] == "member_add" and "contribution" in body:
            contribution = float(body["contribution"])
        else:
            contribution = float(plan.get("contributions", {}).get(investor_id, 0))
        rate = float(pinned_rules.get("guarantee_rates", {}).get("bid", 0))
        held = connection.execute(
            "SELECT COALESCE(SUM(amount),0) AS total FROM guarantees "
            "WHERE application_id=? AND investor_id=? AND guarantee_kind='bid' "
            "AND status IN ('held','partial')",
            (application_id, investor_id)).fetchone()["total"]
        needed = contribution * rate
        if held + 1e-9 < needed:
            raise ConflictError(
                f"入场成员 {investor_id} 投标担保不足：需 {needed:.2f}，实缴 {float(held):.2f}")
        # 新版本的出资计划（旧版本保持不变），新成员出资额写入新版本
        plan_updates = {}
        if task["kind"] == "member_add" and "contribution" in body:
            plan_updates["add_contribution"] = (investor_id, float(body["contribution"]))
        self._append_plan_version(connection, agreement_id, task["task_id"], now, plan_updates)
        # 资产在提单时已按 task_id 预占，会签通过后保留占用（task_id 留痕）
        self._audit(connection, actor_id=actor_id, action=f"membership.{kind}",
                    resource_type="agreement", resource_id=agreement_id,
                    detail={"investor_id": investor_id, "task_id": task["task_id"]})

    def _append_plan_version(self, connection, agreement_id: str, task_id: str, now: str,
                             updates: dict | None = None) -> None:
        agreement = connection.execute("SELECT * FROM agreements WHERE agreement_id=?",
                                       (agreement_id,)).fetchone()
        latest = connection.execute(
            "SELECT plan_json FROM agreement_plans WHERE agreement_id=? ORDER BY version_no DESC LIMIT 1",
            (agreement_id,)).fetchone()
        plan = json_loads(latest["plan_json"])
        members = self._current_members(connection, agreement_id)
        # 依据当前成员重述股比视图
        plan["member_shares"] = {investor_id: item["share"] for investor_id, item in members.items()}
        if updates and "add_contribution" in updates:
            member_id, amount = updates["add_contribution"]
            plan.setdefault("contributions", {})[member_id] = amount
        version_no = agreement["plan_version"] + 1
        connection.execute(
            "INSERT INTO agreement_plans(agreement_id,version_no,plan_json,payload_hash,"
            "created_by_task,created_at) VALUES(?,?,?,?,?,?)",
            (agreement_id, version_no, canonical_json(plan), digest(plan), task_id, now))
        connection.execute("UPDATE agreements SET plan_version=? WHERE agreement_id=?",
                           (version_no, agreement_id))

    def _application_of(self, connection, agreement_id: str) -> str:
        return connection.execute("SELECT application_id FROM agreements WHERE agreement_id=?",
                                  (agreement_id,)).fetchone()["application_id"]

    def _apply_member_remove(self, connection, body, task, actor_id: str) -> None:
        agreement_id = body["agreement_id"]
        investor_id = body["investor_id"]
        outstanding = connection.execute(
            "SELECT COALESCE(SUM(amount),0) AS total FROM obligations "
            "WHERE agreement_id=? AND investor_id=? AND status='open'",
            (agreement_id, investor_id)).fetchone()["total"]
        application_id = self._application_of(connection, agreement_id)
        claimed = self._claim_guarantees(connection, application_id=application_id,
                                         investor_ids=(investor_id,),
                                         target=float(outstanding), actor_id=actor_id,
                                         note="成员退出追索")
        if claimed + 1e-9 < float(outstanding):
            raise ConflictError(
                f"投资人 {investor_id} 尚有未完成义务 {outstanding:.2f}，担保仅覆盖 {claimed:.2f}，"
                "须先履约或补足担保方可退出")
        self._release_remaining(connection, application_id, (investor_id,),
                                actor_id, "成员退出释放剩余担保")
        self._release_assets(connection, application_id=application_id,
                             investor_ids=(investor_id,))
        self._settle_obligations(connection, agreement_id=agreement_id,
                                 investor_ids=(investor_id,), actor_id=actor_id)
        now = self._now()
        connection.execute(
            "INSERT INTO membership_events(event_id,agreement_id,seq,kind,investor_id,"
            "payload_json,actor_id,at) VALUES(?,?,?, 'member_removed', ?,?,?,?)",
            (uuid.uuid4().hex, agreement_id,
             self._membership_next_seq(connection, agreement_id), investor_id,
             canonical_json({"outstanding": float(outstanding), "claimed": claimed}),
             actor_id, now))
        self._append_plan_version(connection, agreement_id, task["task_id"], now)
        self._audit(connection, actor_id=actor_id, action="membership.member_removed",
                    resource_type="agreement", resource_id=agreement_id,
                    detail={"investor_id": investor_id, "claimed": claimed,
                            "outstanding": float(outstanding)})

    def _apply_control_change(self, connection, body, task, actor_id: str) -> None:
        agreement_id = body["agreement_id"]
        new_controller = body["new_controller_id"]
        now = self._now()
        connection.execute("UPDATE agreements SET controller_id=? WHERE agreement_id=?",
                           (new_controller, agreement_id))
        connection.execute(
            "INSERT INTO membership_events(event_id,agreement_id,seq,kind,investor_id,"
            "payload_json,actor_id,at) VALUES(?,?,?, 'control_changed', ?,?,?,?)",
            (uuid.uuid4().hex, agreement_id,
             self._membership_next_seq(connection, agreement_id), new_controller,
             canonical_json({"reason": body.get("reason", "")}), actor_id, now))
        self._audit(connection, actor_id=actor_id, action="membership.control_changed",
                    resource_type="agreement", resource_id=agreement_id,
                    detail={"new_controller": new_controller, "task_id": task["task_id"]})

    def _apply_default_exit(self, connection, body, task, actor_id: str) -> None:
        agreement_id = body["agreement_id"]
        agreement = connection.execute("SELECT * FROM agreements WHERE agreement_id=?",
                                       (agreement_id,)).fetchone()
        if agreement["status"] != "active":
            raise ConflictError("协议已经终止")
        members = self._current_members(connection, agreement_id)
        investor_ids = tuple(members)
        application_id = agreement["application_id"]
        outstanding = connection.execute(
            "SELECT COALESCE(SUM(amount),0) AS total FROM obligations "
            "WHERE agreement_id=? AND status='open'",
            (agreement_id,)).fetchone()["total"]
        claimed = self._claim_guarantees(connection, application_id=application_id,
                                         investor_ids=investor_ids,
                                         target=float(outstanding), actor_id=actor_id,
                                         note="违约退出追索")
        # 释放未被追索的剩余担保
        released = self._release_all_remaining(connection, application_id, actor_id,
                                               "违约退出结清后释放可退担保")
        self._release_assets(connection, application_id=application_id, investor_ids=investor_ids)
        self._settle_obligations(connection, agreement_id=agreement_id,
                                 investor_ids=investor_ids, actor_id=actor_id)
        now = self._now()
        for investor_id in investor_ids:
            connection.execute(
                "INSERT INTO membership_events(event_id,agreement_id,seq,kind,investor_id,"
                "payload_json,actor_id,at) VALUES(?,?,?, 'exited', ?,?,?,?)",
                (uuid.uuid4().hex, agreement_id,
                 self._membership_next_seq(connection, agreement_id), investor_id,
                 canonical_json({"reason": body.get("reason", "default_exit")}), actor_id, now))
        settlement_id = f"set-{uuid.uuid4().hex[:12]}"
        detail = {"outstanding_obligations": float(outstanding), "claimed_from_guarantees": claimed,
                  "released_guarantees": released, "shortfall": max(0.0, float(outstanding) - claimed),
                  "reason": body.get("reason", "default_exit"), "task_id": task["task_id"]}
        connection.execute(
            "INSERT INTO exit_settlements(settlement_id,agreement_id,project_id,reason,"
            "outstanding_amount,claimed_amount,released_amount,detail_json,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (settlement_id, agreement_id, agreement["project_id"], body.get("reason", "default_exit"),
             float(outstanding), claimed, released, canonical_json(detail), actor_id, now))
        connection.execute(
            "INSERT INTO agreement_events(agreement_id,seq,status,actor_id,note,at) "
            "VALUES(?,?,'terminated',?,'违约退出，义务已结清',?)",
            (agreement_id, self._agreement_next_seq(connection, agreement_id), actor_id, now))
        connection.execute("UPDATE agreements SET status='terminated' WHERE agreement_id=?",
                           (agreement_id,))
        connection.execute("UPDATE projects SET status='closed' WHERE project_id=?",
                           (agreement["project_id"],))
        self._audit(connection, actor_id=actor_id, action="exit.settled",
                    resource_type="settlement", resource_id=settlement_id,
                    detail={"agreement_id": agreement_id, **detail})

    def _agreement_next_seq(self, connection, agreement_id: str) -> int:
        return connection.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS next FROM agreement_events WHERE agreement_id=?",
            (agreement_id,)).fetchone()["next"]

    def _claim_guarantees(self, connection, *, application_id, investor_ids, target,
                          actor_id, note) -> float:
        remaining = float(target)
        claimed_total = 0.0
        rows = connection.execute(
            "SELECT * FROM guarantees WHERE application_id=? AND investor_id IN (%s) "
            "AND status IN ('held','partial') ORDER BY CASE guarantee_kind "
            "WHEN 'performance' THEN 0 ELSE 1 END, posted_at, guarantee_id"
            % ",".join("?" for _ in investor_ids),
            (application_id, *investor_ids)).fetchall()
        for row in rows:
            if remaining <= 1e-9:
                break
            available = self._guarantee_available(connection, row["guarantee_id"])
            take = min(available, remaining)
            if take <= 0:
                continue
            seq = connection.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS next FROM guarantee_events WHERE guarantee_id=?",
                (row["guarantee_id"],)).fetchone()["next"]
            connection.execute(
                "INSERT INTO guarantee_events(guarantee_id,seq,action,amount,actor_id,at,note) "
                "VALUES(?,?,'claim',?,?,?,?)",
                (row["guarantee_id"], seq, take, actor_id, self._now(), note))
            new_status = "claimed" if take + 1e-9 >= available else "partial"
            connection.execute("UPDATE guarantees SET status=? WHERE guarantee_id=?",
                               (new_status, row["guarantee_id"]))
            remaining -= take
            claimed_total += take
        return claimed_total

    def _guarantee_available(self, connection, guarantee_id: str) -> float:
        row = connection.execute(
            "SELECT g.amount, COALESCE(SUM(CASE WHEN e.action='claim' THEN e.amount ELSE 0 END),0) "
            "AS claimed FROM guarantees g LEFT JOIN guarantee_events e "
            "ON e.guarantee_id=g.guarantee_id WHERE g.guarantee_id=? GROUP BY g.guarantee_id",
            (guarantee_id,)).fetchone()
        return float(row["amount"]) - float(row["claimed"])

    def _release_remaining(self, connection, application_id, investor_ids, actor_id, note) -> float:
        total = 0.0
        rows = connection.execute(
            "SELECT guarantee_id FROM guarantees WHERE application_id=? AND investor_id IN (%s) "
            "AND status IN ('held','partial')" % ",".join("?" for _ in investor_ids),
            (application_id, *investor_ids)).fetchall()
        for row in rows:
            available = self._guarantee_available(connection, row["guarantee_id"])
            if available <= 1e-9:
                continue
            seq = connection.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS next FROM guarantee_events WHERE guarantee_id=?",
                (row["guarantee_id"],)).fetchone()["next"]
            connection.execute(
                "INSERT INTO guarantee_events(guarantee_id,seq,action,amount,actor_id,at,note) "
                "VALUES(?,?,'release',?,?,?,?)",
                (row["guarantee_id"], seq, available, actor_id, self._now(), note))
            current = connection.execute("SELECT status FROM guarantees WHERE guarantee_id=?",
                                         (row["guarantee_id"],)).fetchone()["status"]
            # 释放的是全部可用余额，释放后该担保不再有可退/可追金额
            connection.execute("UPDATE guarantees SET status='released' WHERE guarantee_id=?",
                               (row["guarantee_id"],))
            total += available
        return total

    def _release_all_remaining(self, connection, application_id, actor_id, note) -> float:
        rows = connection.execute(
            "SELECT DISTINCT investor_id FROM guarantees WHERE application_id=?",
            (application_id,)).fetchall()
        total = 0.0
        for row in rows:
            total += self._release_remaining(connection, application_id, (row["investor_id"],),
                                             actor_id, note)
        return total

    def _release_assets(self, connection, *, application_id, investor_ids) -> None:
        connection.execute(
            "UPDATE asset_engagements SET released_at=? WHERE application_id=? "
            "AND released_at IS NULL AND asset_id IN ("
            "SELECT asset_id FROM pledged_assets WHERE investor_id IN (%s))"
            % ",".join("?" for _ in investor_ids),
            (self._now(), application_id, *investor_ids))

    def _settle_obligations(self, connection, *, agreement_id, investor_ids, actor_id) -> None:
        rows = connection.execute(
            "SELECT obligation_id FROM obligations WHERE agreement_id=? AND investor_id IN (%s) "
            "AND status='open'" % ",".join("?" for _ in investor_ids),
            (agreement_id, *investor_ids)).fetchall()
        for row in rows:
            connection.execute("UPDATE obligations SET status='settled' WHERE obligation_id=?",
                               (row["obligation_id"],))
            seq = connection.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS next FROM obligation_events WHERE obligation_id=?",
                (row["obligation_id"],)).fetchone()["next"]
            connection.execute(
                "INSERT INTO obligation_events(obligation_id,seq,status,actor_id,at,note) "
                "VALUES(?,?,'settled',?,?,'退出结算')",
                (row["obligation_id"], seq, actor_id, self._now()))

    # ------------------------------------------------------------------
    # 义务登记与履约
    # ------------------------------------------------------------------

    def register_obligation(self, *, request_id: str, actor_id: str, agreement_id: str,
                            investor_id: str, phase: int, title: str, amount: float,
                            due_at: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "investor_id": investor_id,
                   "phase": phase, "title": title, "amount": amount, "due_at": due_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator", "reviewer")
            agreement = connection.execute("SELECT * FROM agreements WHERE agreement_id=?",
                                           (agreement_id,)).fetchone()
            if agreement is None:
                raise NotFoundError("协议不存在")
            if not self._current_members(connection, agreement_id).get(investor_id):
                raise ValidationError("义务承担方必须是当前联合体成员")
            if not isinstance(phase, int) or phase < 1:
                raise ValidationError("phase 必须是不小于 1 的整数")
            if not isinstance(amount, (int, float)) or amount < 0:
                raise ValidationError("amount 必须是非负数")

            def create():
                obligation_id = f"obl-{uuid.uuid4().hex[:12]}"
                connection.execute(
                    "INSERT INTO obligations(obligation_id,agreement_id,investor_id,phase,title,"
                    "amount,due_at,status,created_at) VALUES(?,?,?,?,?,?,?,'open',?)",
                    (obligation_id, agreement_id, investor_id, phase, str(title),
                     float(amount), due_at, self._now()))
                self._audit(connection, actor_id=actor_id, action="obligation.registered",
                            resource_type="obligation", resource_id=obligation_id,
                            detail={"agreement_id": agreement_id, "investor_id": investor_id,
                                    "amount": float(amount), "phase": phase})
                return {"resource_type": "obligation", "resource_id": obligation_id,
                        "obligation_id": obligation_id, "status": "open"}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_obligation", payload=payload, create=create)

    def fulfill_obligation(self, *, request_id: str, actor_id: str,
                           obligation_id: str, note: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "obligation_id": obligation_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator", "reviewer")
            row = connection.execute("SELECT * FROM obligations WHERE obligation_id=?",
                                     (obligation_id,)).fetchone()
            if row is None:
                raise NotFoundError("义务不存在")
            if row["status"] != "open":
                raise ConflictError("义务已经办结")

            def create():
                connection.execute("UPDATE obligations SET status='fulfilled' WHERE obligation_id=?",
                                   (obligation_id,))
                connection.execute(
                    "INSERT INTO obligation_events(obligation_id,seq,status,actor_id,at,note) "
                    "VALUES(?,1,'fulfilled',?,?,?)",
                    (obligation_id, actor_id, self._now(), note))
                self._audit(connection, actor_id=actor_id, action="obligation.fulfilled",
                            resource_type="obligation", resource_id=obligation_id, detail={})
                return {"resource_type": "obligation", "resource_id": obligation_id,
                        "obligation_id": obligation_id, "status": "fulfilled"}

            return self._idempotent(connection, request_id=request_id,
                                    action="fulfill_obligation", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 时点还原：规则、证据、批准链
    # ------------------------------------------------------------------

    def project_snapshot(self, project_id: str, at: str | None = None) -> dict[str, Any]:
        at = at or self._now()
        connection = self.database.connection
        project = connection.execute("SELECT * FROM projects WHERE project_id=?",
                                     (project_id,)).fetchone()
        if project is None:
            raise NotFoundError("项目不存在")
        version_row = connection.execute(
            "SELECT * FROM project_versions WHERE project_id=? AND created_at<=? "
            "ORDER BY version_no DESC LIMIT 1", (project_id, at)).fetchone()
        rule_row = connection.execute(
            "SELECT * FROM rule_versions WHERE effective_at<=? ORDER BY ordinal DESC LIMIT 1",
            (at,)).fetchone()
        applications = []
        app_rows = connection.execute(
            "SELECT * FROM applications WHERE project_id=? AND submitted_at<=?",
            (project_id, at)).fetchall()
        for app in app_rows:
            status = app["status"]
            events = connection.execute(
                "SELECT status,actor_id,note,at FROM application_events "
                "WHERE application_id=? AND at<=? ORDER BY seq", (app["application_id"], at)).fetchall()
            if events:
                status = events[-1]["status"]
            members = connection.execute(
                "SELECT investor_id,member_role,share,entry_phase,is_private "
                "FROM application_members WHERE application_id=? ORDER BY investor_id",
                (app["application_id"],)).fetchall()
            evidences = connection.execute(
                "SELECT evidence_id,investor_id,document_hash FROM application_evidence "
                "WHERE application_id=?", (app["application_id"],)).fetchall()
            applications.append({"application_id": app["application_id"], "app_seq": app["app_seq"],
                                 "status_at": status, "lead_investor_id": app["lead_investor_id"],
                                 "pinned_rule_version_id": app["rule_version_id"],
                                 "project_version": app["project_version"],
                                 "submitted_at": app["submitted_at"],
                                 "members": [dict(m) for m in members],
                                 "evidence": [dict(e) for e in evidences],
                                 "status_history": [dict(e) for e in events]})
        agreements = []
        agr_rows = connection.execute(
            "SELECT * FROM agreements WHERE project_id=? AND signed_at<=?", (project_id, at)).fetchall()
        for agr in agr_rows:
            plans = connection.execute(
                "SELECT version_no,plan_json,payload_hash,created_by_task,created_at "
                "FROM agreement_plans WHERE agreement_id=? AND created_at<=? ORDER BY version_no",
                (agr["agreement_id"], at)).fetchall()
            membership = connection.execute(
                "SELECT seq,kind,investor_id,payload_json,actor_id,at FROM membership_events "
                "WHERE agreement_id=? AND at<=? ORDER BY seq",
                (agr["agreement_id"], at)).fetchall()
            guarantees = connection.execute(
                "SELECT g.guarantee_id,g.investor_id,g.guarantee_kind,g.amount,g.posted_at,"
                "g.status FROM guarantees g WHERE g.application_id=? ORDER BY g.posted_at",
                (agr["application_id"],)).fetchall()
            guarantee_events = connection.execute(
                "SELECT ge.guarantee_id,ge.seq,ge.action,ge.amount,ge.actor_id,ge.at,ge.note "
                "FROM guarantee_events ge JOIN guarantees g ON g.guarantee_id=ge.guarantee_id "
                "WHERE g.application_id=? AND ge.at<=? ORDER BY ge.guarantee_id,ge.seq",
                (agr["application_id"], at)).fetchall()
            obligations = connection.execute(
                "SELECT obligation_id,investor_id,phase,title,amount,due_at,status FROM obligations "
                "WHERE agreement_id=? ORDER BY phase,obligation_id", (agr["agreement_id"],)).fetchall()
            # 重放成员状态到时点
            members_at: dict[str, dict] = {}
            for event in membership:
                payload = json_loads(event["payload_json"])
                if event["kind"] in ("admitted", "member_added", "phase_entered"):
                    members_at[event["investor_id"]] = payload
                else:
                    members_at.pop(event["investor_id"], None)
            agreements.append({"agreement_id": agr["agreement_id"],
                               "pinned_rule_version_id": agr["rule_version_id"],
                               "controller_id": agr["controller_id"],
                               "lead_investor_id": agr["lead_investor_id"],
                               "signed_at": agr["signed_at"],
                               "plan_versions": [{"version_no": p["version_no"],
                                                  "payload_hash": p["payload_hash"],
                                                  "created_by_task": p["created_by_task"],
                                                  "created_at": p["created_at"],
                                                  "plan": json_loads(p["plan_json"])} for p in plans],
                               "membership_events": [{"seq": e["seq"], "kind": e["kind"],
                                                      "investor_id": e["investor_id"],
                                                      "payload": json_loads(e["payload_json"]),
                                                      "actor_id": e["actor_id"], "at": e["at"]}
                                                     for e in membership],
                               "members_at": members_at,
                               "guarantees": [dict(g) for g in guarantees],
                               "guarantee_events": [dict(e) for e in guarantee_events],
                               "obligations": [dict(o) for o in obligations]})
        # 批准链：截至时点已办结的会签事项与全部签名
        tasks = connection.execute(
            "SELECT t.* FROM review_tasks t WHERE t.project_id=? AND t.created_at<=? "
            "ORDER BY t.task_seq", (project_id, at)).fetchall()
        approval_chain = []
        for task in tasks:
            signatures = connection.execute(
                "SELECT role,actor_id,decision,note,signed_at FROM review_signatures "
                "WHERE task_id=? AND signed_at<=? ORDER BY role",
                (task["task_id"], at)).fetchall()
            approval_chain.append({"task_id": task["task_id"], "task_seq": task["task_seq"],
                                   "kind": task["kind"], "status": task["status"],
                                   "submitter": task["submitter"], "created_at": task["created_at"],
                                   "signatures": [dict(s) for s in signatures]})
        settlements = connection.execute(
            "SELECT settlement_id,agreement_id,reason,outstanding_amount,claimed_amount,"
            "released_amount,detail_json,created_at FROM exit_settlements "
            "WHERE project_id=? AND created_at<=? ORDER BY created_at", (project_id, at)).fetchall()
        return {
            "as_of": at,
            "project": {"project_id": project_id, "current_version": project["current_version"],
                        "status": project["status"]},
            "project_version_at": None if version_row is None else {
                "version_no": version_row["version_no"], "name": version_row["name"],
                "detail": json_loads(version_row["detail_json"]),
                "rule_version_id": version_row["rule_version_id"],
                "payload_hash": version_row["payload_hash"],
                "created_at": version_row["created_at"]},
            "rule_version_at": None if rule_row is None else {
                "version_id": rule_row["version_id"], "ordinal": rule_row["ordinal"],
                "rules": json_loads(rule_row["rules_json"]),
                "content_hash": rule_row["content_hash"],
                "effective_at": rule_row["effective_at"]},
            "applications": applications,
            "agreements": agreements,
            "approval_chain": approval_chain,
            "settlements": [{"settlement_id": s["settlement_id"], "agreement_id": s["agreement_id"],
                             "reason": s["reason"], "outstanding_amount": s["outstanding_amount"],
                             "claimed_amount": s["claimed_amount"],
                             "released_amount": s["released_amount"],
                             "detail": json_loads(s["detail_json"]), "created_at": s["created_at"]}
                            for s in settlements],
        }

    # ------------------------------------------------------------------
    # 候选方案比较：民营参与比例与交付风险
    # ------------------------------------------------------------------

    def compare_candidates(self, project_id: str, candidates: list[dict[str, Any]],
                           rule_version_id: str | None = None) -> dict[str, Any]:
        if not isinstance(candidates, list) or not candidates:
            raise ValidationError("candidates 必须是非空数组")
        connection = self.database.connection
        if connection.execute("SELECT 1 FROM projects WHERE project_id=?",
                              (project_id,)).fetchone() is None:
            raise NotFoundError("项目不存在")
        if rule_version_id is None:
            rule_row = self._current_rule(connection)
        else:
            rule_row = connection.execute("SELECT * FROM rule_versions WHERE version_id=?",
                                          (rule_version_id,)).fetchone()
            if rule_row is None:
                raise NotFoundError("规则版本不存在")
        rules = json_loads(rule_row["rules_json"])
        results = []
        for candidate in candidates:
            label = candidate.get("name", candidate.get("candidate_id", "candidate"))
            members = candidate.get("members", [])
            private_ratio = 0.0
            total = 0.0
            lead_share = 0.0
            phase_count: dict[int, float] = {}
            for member in members:
                share = float(member["share"])
                total += share
                if member.get("is_private", False):
                    private_ratio += share
                if member.get("member_role") == "lead":
                    lead_share += share
                phase_count[int(member.get("entry_phase", 1))] = \
                    phase_count.get(int(member.get("entry_phase", 1)), 0.0) + share
            if abs(total - 1.0) > 1e-9:
                raise ValidationError(f"候选方案 {label} 股比之和必须为 1")
            evidence_ids = candidate.get("evidence_ids", [])
            evidence_kinds = set()
            evidence_ok = True
            for evidence_id in evidence_ids:
                row, status, valid = self._evidence_state(connection, evidence_id)
                evidence_kinds.add(row["evidence_kind"])
                if not valid:
                    evidence_ok = False
            required = set(rules.get("required_evidence_kinds", []))
            evidence_coverage = (len(required & evidence_kinds) / len(required)) if required else 1.0
            contributions = candidate.get("contributions", {})
            guarantees = candidate.get("guarantees", {})
            bid_rate = float(rules.get("guarantee_rates", {}).get("bid", 0))
            perf_rate = float(rules.get("guarantee_rates", {}).get("performance", 0))
            uncovered = 0.0
            total_contribution = 0.0
            for investor_id, amount in contributions.items():
                amount = float(amount)
                total_contribution += amount
                provided = float(guarantees.get(investor_id, 0))
                needed = amount * (bid_rate + perf_rate)
                if provided + 1e-9 < needed:
                    uncovered += needed - provided
            guarantee_coverage = 1.0 if total_contribution <= 0 else \
                max(0.0, 1.0 - uncovered / (total_contribution * (bid_rate + perf_rate) or 1.0))
            concentration = lead_share
            phased = len(phase_count)
            meets_policy = private_ratio + 1e-9 >= float(rules["min_private_ratio"]) \
                and evidence_coverage >= 1.0 and evidence_ok \
                and guarantee_coverage + 1e-9 >= 1.0
            # 风险分：政策不达标、证据缺口、担保缺口、控股集中、分期分散都会抬升风险
            risk = 0.0
            risk += 0.35 * (1.0 - guarantee_coverage)
            risk += 0.25 * (1.0 - evidence_coverage)
            risk += 0.20 * max(0.0, concentration - 0.5) * 2.0
            risk += 0.10 * (phased - 1) * 0.5
            if private_ratio + 1e-9 < float(rules["min_private_ratio"]):
                risk += 0.20
            risk = round(min(1.0, risk), 6)
            results.append({"name": label, "private_ratio": round(private_ratio, 6),
                            "meets_min_private_ratio": private_ratio + 1e-9
                            >= float(rules["min_private_ratio"]),
                            "evidence_coverage": round(evidence_coverage, 6),
                            "evidence_all_valid": evidence_ok,
                            "guarantee_coverage": round(guarantee_coverage, 6),
                            "lead_concentration": round(concentration, 6),
                            "entry_phases": sorted(phase_count),
                            "meets_policy": meets_policy,
                            "delivery_risk": risk})
        results.sort(key=lambda item: (not item["meets_policy"], item["delivery_risk"],
                                       -item["private_ratio"]))
        return {"project_id": project_id, "rule_version_id": rule_row["version_id"],
                "rule_content_hash": rule_row["content_hash"], "candidates": results,
                "recommended": results[0]["name"] if results and results[0]["meets_policy"] else None}

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_application(self, application_id: str) -> dict[str, Any]:
        connection = self.database.connection
        app = connection.execute("SELECT * FROM applications WHERE application_id=?",
                                 (application_id,)).fetchone()
        if app is None:
            raise NotFoundError("申请不存在")
        members = connection.execute(
            "SELECT investor_id,member_role,share,entry_phase,is_private "
            "FROM application_members WHERE application_id=? ORDER BY investor_id",
            (application_id,)).fetchall()
        evidence = connection.execute(
            "SELECT evidence_id,investor_id,document_hash FROM application_evidence "
            "WHERE application_id=?", (application_id,)).fetchall()
        events = connection.execute(
            "SELECT seq,status,actor_id,note,at FROM application_events WHERE application_id=? "
            "ORDER BY seq", (application_id,)).fetchall()
        return {"application_id": application_id, "project_id": app["project_id"],
                "project_version": app["project_version"],
                "rule_version_id": app["rule_version_id"],
                "lead_investor_id": app["lead_investor_id"], "status": app["status"],
                "plan": json_loads(app["plan_json"]), "submitted_at": app["submitted_at"],
                "members": [dict(m) for m in members],
                "evidence": [dict(e) for e in evidence],
                "events": [dict(e) for e in events]}

    def get_agreement(self, agreement_id: str) -> dict[str, Any]:
        connection = self.database.connection
        agr = connection.execute("SELECT * FROM agreements WHERE agreement_id=?",
                                 (agreement_id,)).fetchone()
        if agr is None:
            raise NotFoundError("协议不存在")
        members = self._current_members(connection, agreement_id)
        settlements = connection.execute(
            "SELECT settlement_id,reason,outstanding_amount,claimed_amount,released_amount,"
            "created_at FROM exit_settlements WHERE agreement_id=? ORDER BY created_at",
            (agreement_id,)).fetchall()
        return {"agreement_id": agreement_id, "project_id": agr["project_id"],
                "application_id": agr["application_id"],
                "rule_version_id": agr["rule_version_id"],
                "controller_id": agr["controller_id"],
                "lead_investor_id": agr["lead_investor_id"],
                "plan_version": agr["plan_version"], "status": agr["status"],
                "signed_at": agr["signed_at"],
                "current_members": [{"investor_id": k, **v} for k, v in
                                    sorted(members.items())],
                "settlements": [dict(s) for s in settlements]}


def json_loads(value: str):
    import json
    return json.loads(value)
