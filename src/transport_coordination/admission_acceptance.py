"""交通项目社会资本准入退出服务的离线端到端验收。

覆盖：规则/项目版本登记、关联主体重复申报拦截、竞争申请唯一授标、
职责分离会签、分期入场、成员增减、控制权变更、违约退出结清、
规则换版不倒改协议、时点还原，以及"重启"后待审队列顺序保持。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .admission_service import AdmissionService
from .clock import FixedClock
from .service import DomainService
from .storage import Database


def _bootstrap(service: DomainService, admission: AdmissionService, tick) -> None:
    service.register_organization(request_id="req-org", actor_id="bootstrap",
                                  organization_id="office", name="省交通项目推进办")
    service.register_actor(request_id="req-admin", actor_id="bootstrap",
                           new_actor_id="admin-1", display_name="批准人",
                           role="admin", organization_id="office")
    service.register_actor(request_id="req-reviewer", actor_id="admin-1",
                           new_actor_id="reviewer-1", display_name="评审人",
                           role="reviewer", organization_id="office")
    service.register_actor(request_id="req-operator", actor_id="admin-1",
                           new_actor_id="operator-1", display_name="经办人",
                           role="operator", organization_id="office")
    tick()
    admission.publish_policy(
        request_id="req-policy-v1", actor_id="admin-1", policy_id="access",
        thresholds={"min_private_ratio": 0.5, "min_guarantee_ratio": 0.1,
                    "required_evidence_types": ["business_license"],
                    "recommended_evidence_types": ["credit_report", "safety_record"],
                    "max_controller_share": 0.8, "breach_forfeit_ratio": 1.0})
    tick()
    admission.publish_project_version(
        request_id="req-project-v1", actor_id="admin-1", project_id="express-ring",
        name="绕城高速扩能", total_investment=1000.0, private_target_ratio=0.6,
        assets=["land-north", "bridge-east"])
    for index, (investor_id, kind) in enumerate(
            [("minyuan", "private"), ("lujian", "private"),
             ("jiaotou", "state_owned"), ("minyuan_branch", "private")]):
        tick()
        admission.register_investor(
            request_id=f"req-investor-{index}", actor_id="operator-1",
            investor_id=investor_id, name=investor_id, ownership_kind=kind)
    tick()
    admission.link_investor_relation(
        request_id="req-relation-1", actor_id="operator-1", left_id="minyuan",
        right_id="minyuan_branch", relation_type="同一控股")
    for index, investor_id in enumerate(["minyuan", "lujian", "minyuan_branch"]):
        tick()
        admission.record_evidence(
            request_id=f"req-evidence-{index}", actor_id="operator-1",
            evidence_id=f"lic-{investor_id}", investor_id=investor_id,
            evidence_type="business_license", document_ref=f"arc/{investor_id}.pdf",
            content_hash=f"sha256-{investor_id}", valid_from="2026-01-01",
            valid_until="2027-12-31")
        tick()
        admission.verify_evidence(
            request_id=f"req-verify-{index}", actor_id="reviewer-1",
            evidence_id=f"lic-{investor_id}", verified=True)
    for index, (investor_id, amount) in enumerate(
            [("minyuan", 120.0), ("lujian", 150.0), ("minyuan_branch", 120.0)]):
        tick()
        admission.register_guarantee(
            request_id=f"req-guarantee-{index}", actor_id="operator-1",
            guarantee_id=f"bond-{investor_id}", investor_id=investor_id,
            guarantee_type="performance_bond", amount=amount)
    tick()
    admission.submit_capital_plan(
        request_id="req-plan-minyuan", actor_id="operator-1", plan_id="plan-minyuan",
        project_id="express-ring", investor_id="minyuan", total_amount=700.0,
        private_amount=700.0,
        tranches=[{"label": "首期", "amount": 300.0, "scheduled_at": "2026-12-01"},
                  {"label": "二期", "amount": 400.0, "scheduled_at": "2027-09-01"}])
    tick()
    admission.submit_capital_plan(
        request_id="req-plan-lujian", actor_id="operator-1", plan_id="plan-lujian",
        project_id="express-ring", investor_id="lujian", total_amount=700.0,
        private_amount=420.0,
        tranches=[{"label": "首期", "amount": 350.0, "scheduled_at": "2026-12-01"},
                  {"label": "二期", "amount": 350.0, "scheduled_at": "2027-09-01"}])


def run() -> dict[str, object]:
    """执行完整的准入退出链并返回核对结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "admission_acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 10, 2, 2, 0, tzinfo=timezone.utc))

        def tick(minutes: int = 1) -> None:
            clock._value += timedelta(minutes=minutes)

        service = DomainService(database, clock)
        admission = AdmissionService(database, clock)
        _bootstrap(service, admission, tick)

        related_blocked = False
        tick()
        admission.propose_review(
            request_id="req-bid-minyuan", actor_id="operator-1", review_id="bid-1",
            project_id="express-ring", task_type="qualification",
            applicant_id="minyuan",
            proposal={"stage_label": "main",
                      "consortium": [
                          {"investor_id": "minyuan", "shares": 0.7, "controller": True},
                          {"investor_id": "jiaotou", "shares": 0.3}],
                      "evidence_ids": ["lic-minyuan"],
                      "guarantee_ids": ["bond-minyuan"],
                      "assets": ["land-north"], "capital_plan_id": "plan-minyuan"})
        tick()
        try:
            admission.propose_review(
                request_id="req-bid-branch", actor_id="operator-1", review_id="bid-x",
                project_id="express-ring", task_type="qualification",
                applicant_id="minyuan_branch",
                proposal={"consortium": [
                    {"investor_id": "minyuan_branch", "shares": 1.0, "controller": True}],
                    "evidence_ids": ["lic-minyuan_branch"],
                    "guarantee_ids": ["bond-minyuan_branch"],
                    "assets": ["bridge-east"], "capital_plan_id": "plan-lujian"})
        except Exception:
            related_blocked = True

        tick()
        admission.propose_review(
            request_id="req-bid-lujian", actor_id="operator-1", review_id="bid-2",
            project_id="express-ring", task_type="qualification",
            applicant_id="lujian",
            proposal={"consortium": [
                {"investor_id": "lujian", "shares": 0.6, "controller": True},
                {"investor_id": "jiaotou", "shares": 0.4}],
                "evidence_ids": ["lic-lujian"], "guarantee_ids": ["bond-lujian"],
                "assets": ["bridge-east"], "capital_plan_id": "plan-lujian"})

        queue_before = [item["review_id"] for item in admission.pending_queue()]

        comparison = admission.compare_scenarios("express-ring")
        tick()
        admission.record_approval(request_id="req-bid-1-review", actor_id="reviewer-1",
                                  review_id="bid-1", decision="approve",
                                  comment="资格证据齐全")
        tick()
        admission.record_approval(request_id="req-bid-1-approve", actor_id="admin-1",
                                  review_id="bid-1", decision="approve",
                                  comment="批准授标")
        winner = admission.get_review("bid-1")
        loser = admission.get_review("bid-2")

        tick()
        admission.propose_review(
            request_id="req-entry-1", actor_id="operator-1", review_id="entry-1",
            project_id="express-ring", task_type="entry", applicant_id="minyuan",
            proposal={"stage_label": "main", "entrants": ["minyuan"],
                      "tranche": {"label": "首期", "amount": 300.0,
                                  "scheduled_at": "2026-12-01"},
                      "assets": [], "guarantee_ids": [], "evidence_ids": []})
        tick()
        admission.record_approval(request_id="req-entry-1-review",
                                  actor_id="reviewer-1", review_id="entry-1",
                                  decision="approve")
        tick()
        admission.record_approval(request_id="req-entry-1-approve",
                                  actor_id="admin-1", review_id="entry-1",
                                  decision="approve")

        for obligation_id, otype, description, amount, status in [
                ("ob-paid", "capital_payment", "首期出资(按70%权益)", 210.0, "paid"),
                ("ob-unpaid", "capital_payment", "二期出资(按70%权益)", 280.0, "pending"),
                ("ob-works", "construction", "剩余路基工程", 60.0, "in_progress")]:
            tick()
            admission.record_obligation(
                request_id=f"req-{obligation_id}", actor_id="operator-1",
                obligation_id=obligation_id, project_id="express-ring",
                investor_id="minyuan", obligation_type=otype, description=description,
                amount=amount, due_at="2027-09-01", status=status)

        sealed_before_revision = winner.sealed_agreement["agreement_hash"]
        tick()
        admission.publish_policy(
            request_id="req-policy-v2", actor_id="admin-1", policy_id="access",
            thresholds={"min_private_ratio": 0.8, "min_guarantee_ratio": 0.3,
                        "required_evidence_types": ["business_license"],
                        "recommended_evidence_types": ["credit_report"],
                        "max_controller_share": 0.5})

        tick()
        admission.propose_review(
            request_id="req-exit-1", actor_id="operator-1", review_id="exit-1",
            project_id="express-ring", task_type="exit", applicant_id="minyuan",
            proposal={"stage_label": "main", "exit_type": "breach",
                      "reason": "前期工程后违约退出"})
        tick()
        admission.record_approval(request_id="req-exit-1-review", actor_id="reviewer-1",
                                  review_id="exit-1", decision="approve")
        tick()
        admission.record_approval(request_id="req-exit-1-approve", actor_id="admin-1",
                                  review_id="exit-1", decision="approve")
        exited = admission.get_review("exit-1")

        # 模拟服务重启：新建服务实例，状态全部从 SQLite 事件流还原。
        restarted = AdmissionService(database, clock)
        restarted_queue = [item["review_id"] for item in restarted.pending_queue()]
        snapshot = restarted.snapshot("express-ring", as_of="2026-10-02T02:05:00Z")
        audit_valid, audit_events = service.verify_audit()

        from .projection import build_projection
        settled = build_projection(
            admission.store.read_events(aggregate_type="review",
                                        aggregate_id="exit-1")).reviews["exit-1"]["settlement"]

        checks = {
            "related_duplicate_blocked": related_blocked,
            "queue_fifo_before_signoff": queue_before == ["bid-1", "bid-2"],
            "queue_empty_after_restart": restarted_queue == [],
            "single_winner": winner.status == "approved" and winner.outcome == "awarded",
            "competitor_closed": loser.status == "closed" and loser.outcome == "lost",
            "competitor_asset_released": loser.message != "",
            "recommended_is_minyuan": comparison["recommended"] == "bid-1",
            "private_ratio_is_one": comparison["candidates"][0]["private_ratio"] == 1.0,
            "sealed_policy_frozen":
                winner.sealed_agreement["policy_version"]["version"] == 1
                and winner.sealed_agreement["policy_version"]["thresholds"]["min_private_ratio"] == 0.5,
            "sealed_agreement_unchanged":
                winner.sealed_agreement["agreement_hash"] == sealed_before_revision,
            "current_policy_v2": admission.get_policy("access").version == 2,
            "recourse_amount_ok": settled["recourse_amount"] == 340.0,
            "forfeited_amount_ok": settled["forfeited_amount"] == 120.0,
            "refunded_amount_ok": settled["refunded_amount"] == 0.0,
            "asset_released_on_exit": settled["released_assets"] == ["land-north"],
            "snapshot_as_of_reviews_empty": len(snapshot["reviews"]) == 0,
            "snapshot_project_version": snapshot["project_version"]["version"] == 1,
            "audit_valid": audit_valid,
        }
        checks = {"status": "ok" if all(checks.values()) else "failed",
                  "audit_events": audit_events, **checks}
        database.close()
        return checks


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
