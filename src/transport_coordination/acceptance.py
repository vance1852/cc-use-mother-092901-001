"""运行基础服务的离线端到端验收。

第一段验收原有登记、幂等与审计链；第二段验收社会资本准入退出：
规则换版、唯一竞争、职责分离会签、控制权变更、违约退出结算与时点还原。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .participation import ParticipationService
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整登记链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc))
        service = DomainService(database, clock)
        participation = ParticipationService(database, clock)
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范运营机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="运营负责人", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号交通节点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="operator_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="operator_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        participation_result = _run_participation(participation, service)
        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed,
                  "participation": participation_result}
        database.close()
        return result


def _run_participation(participation: ParticipationService, service: DomainService) -> dict[str, object]:
    """跑通社会资本准入、竞争、会签、控制权变更与违约退出。"""

    service.register_actor(request_id="req-reviewer", actor_id="admin-001", new_actor_id="reviewer-001",
                           display_name="资格审查员", role="reviewer", organization_id="org-001")
    participation.publish_rule_version(request_id="req-rules", actor_id="admin-001",
                                       rules={"min_private_ratio": 0.3,
                                              "required_evidence_kinds": ["business_license", "qualification"],
                                              "guarantee_rates": {"bid": 0.1, "performance": 0.2}})
    participation.create_project(request_id="req-project", actor_id="operator-001", project_id="project-001")
    for investor_id, name, kind in [("inv-priv", "民营建设", "private"),
                                    ("inv-soe", "省属路桥", "state_owned")]:
        participation.register_investor(request_id=f"req-{investor_id}", actor_id="operator-001",
                                        investor_id=investor_id, name=name, kind=kind)
    for evidence_id, investor_id, kind in [
            ("ev-lic-p", "inv-priv", "business_license"),
            ("ev-qua-p", "inv-priv", "qualification"),
            ("ev-lic-s", "inv-soe", "business_license"),
            ("ev-qua-s", "inv-soe", "qualification")]:
        participation.register_evidence(request_id=f"req-reg-{evidence_id}", actor_id="operator-001",
                                        evidence_id=evidence_id, investor_id=investor_id,
                                        evidence_kind=kind, document_hash=f"hash-{evidence_id}-0000",
                                        issued_at="2026-01-01T00:00:00Z")
        participation.verify_evidence(request_id=f"req-verify-{evidence_id}", actor_id="reviewer-001",
                                      evidence_id=evidence_id)
    participation.register_asset(request_id="req-asset", actor_id="operator-001", asset_id="asset-001",
                                 investor_id="inv-priv", name="盾构设备", assessed_value=5000.0)
    participation.submit_application(
        request_id="req-app", actor_id="operator-001", application_id="app-001",
        project_id="project-001", lead_investor_id="inv-priv",
        members=[{"investor_id": "inv-priv", "member_role": "lead", "share": 0.4, "entry_phase": 1},
                 {"investor_id": "inv-soe", "member_role": "member", "share": 0.6, "entry_phase": 1}],
        contributions={"inv-priv": 4000.0, "inv-soe": 6000.0},
        evidence_ids=["ev-lic-p", "ev-qua-p", "ev-lic-s", "ev-qua-s"],
        assets=[{"asset_id": "asset-001", "amount": 4000.0}])
    for guarantee_id, investor_id, amount in [("g-bid-p", "inv-priv", 400.0),
                                              ("g-bid-s", "inv-soe", 600.0)]:
        participation.post_guarantee(request_id=f"req-{guarantee_id}", actor_id="operator-001",
                                    guarantee_id=guarantee_id, application_id="app-001",
                                    investor_id=investor_id, guarantee_kind="bid", amount=amount)
    task = participation.submit_review_task(request_id="req-admission", actor_id="operator-001",
                                            kind="admission",
                                            payload={"application_id": "app-001"})
    participation.sign_review_task(request_id="req-admission-r", actor_id="reviewer-001",
                                   task_id=task["task_id"], decision="approved")
    participation.sign_review_task(request_id="req-admission-a", actor_id="admin-001",
                                   task_id=task["task_id"], decision="approved")
    application = participation.get_application("app-001")
    agreement = participation.get_agreement("agr-app-001")

    for guarantee_id, investor_id, amount in [("g-perf-p", "inv-priv", 800.0),
                                              ("g-perf-s", "inv-soe", 1200.0)]:
        participation.post_guarantee(request_id=f"req-{guarantee_id}", actor_id="operator-001",
                                    guarantee_id=guarantee_id, application_id="app-001",
                                    investor_id=investor_id, guarantee_kind="performance",
                                    amount=amount)
    participation.register_obligation(request_id="req-obligation", actor_id="operator-001",
                                      agreement_id="agr-app-001", investor_id="inv-priv", phase=1,
                                      title="路基工程", amount=500.0, due_at="2027-01-01T00:00:00Z")
    control = participation.submit_review_task(request_id="req-control", actor_id="operator-001",
                                               kind="control_change",
                                               payload={"agreement_id": "agr-app-001",
                                                        "new_controller_id": "inv-soe"})
    participation.sign_review_task(request_id="req-control-r", actor_id="reviewer-001",
                                   task_id=control["task_id"], decision="approved")
    participation.sign_review_task(request_id="req-control-a", actor_id="admin-001",
                                   task_id=control["task_id"], decision="approved")
    exit_task = participation.submit_review_task(request_id="req-exit", actor_id="operator-001",
                                                 kind="default_exit",
                                                 payload={"agreement_id": "agr-app-001",
                                                          "reason": "逾期未完工"})
    participation.sign_review_task(request_id="req-exit-r", actor_id="reviewer-001",
                                   task_id=exit_task["task_id"], decision="approved")
    participation.sign_review_task(request_id="req-exit-a", actor_id="admin-001",
                                   task_id=exit_task["task_id"], decision="approved")
    terminated = participation.get_agreement("agr-app-001")
    settlement = terminated["settlements"][0]
    snapshot = participation.project_snapshot("project-001")
    return {"selected": application["status"] == "selected",
            "agreement_status": terminated["status"],
            "controller": terminated["controller_id"],
            "pinned_rule_version": agreement["rule_version_id"],
            "claimed_amount": settlement["claimed_amount"],
            "released_amount": settlement["released_amount"],
            "approval_chain_tasks": len(snapshot["approval_chain"])}


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    participation = result["participation"]
    ok = (result["status"] == "ok" and result["audit_valid"]
          and participation["selected"]
          and participation["agreement_status"] == "terminated"
          and participation["claimed_amount"] == 500.0
          and participation["released_amount"] == 2500.0
          and participation["approval_chain_tasks"] == 3)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
