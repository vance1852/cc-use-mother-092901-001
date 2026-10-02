"""准入退出域的单元测试：覆盖会签、互斥、结清、时点还原与重启顺序。"""

import unittest
from datetime import datetime, timedelta, timezone

from transport_coordination.admission_service import AdmissionService
from transport_coordination.clock import FixedClock
from transport_coordination.errors import (
    ConflictError, PermissionDenied, ValidationError,
)
from transport_coordination.service import DomainService
from transport_coordination.storage import Database


class AdmissionCase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(datetime(2026, 10, 2, tzinfo=timezone.utc))
        self.service = DomainService(self.database, self.clock)
        self.admission = AdmissionService(self.database, self.clock)
        self._seed_actors()
        self._seed_registry()

    def tearDown(self):
        self.database.close()

    def tick(self, minutes: int = 1):
        self.clock._value += timedelta(minutes=minutes)

    def _seed_actors(self):
        self.service.register_organization(request_id="org-01", actor_id="bootstrap",
                                           organization_id="office", name="推进办")
        self.service.register_actor(request_id="act-adm", actor_id="bootstrap",
                                    new_actor_id="admin-1", display_name="批准人",
                                    role="admin", organization_id="office")
        self.service.register_actor(request_id="act-rev", actor_id="admin-1",
                                    new_actor_id="rev-1", display_name="评审人",
                                    role="reviewer", organization_id="office")
        self.service.register_actor(request_id="act-op", actor_id="admin-1",
                                    new_actor_id="op-1", display_name="经办人",
                                    role="operator", organization_id="office")
        self.service.register_actor(request_id="act-au", actor_id="admin-1",
                                    new_actor_id="au-1", display_name="审计员",
                                    role="auditor", organization_id="office")

    def _seed_registry(self):
        self.tick()
        self.admission.publish_policy(
            request_id="pol-v1", actor_id="admin-1", policy_id="rules",
            thresholds={"min_private_ratio": 0.5, "min_guarantee_ratio": 0.1,
                        "required_evidence_types": ["business_license"],
                        "recommended_evidence_types": ["credit_report"],
                        "max_controller_share": 0.8})
        self.tick()
        self.admission.publish_project_version(
            request_id="prj-v1", actor_id="admin-1", project_id="ring",
            name="环线", total_investment=1000.0, private_target_ratio=0.6,
            assets=["asset-1", "asset-2", "asset-3"])
        for index, (investor_id, kind) in enumerate(
                [("inv-a", "private"), ("inv-b", "private"),
                 ("inv-c", "state_owned"), ("inv-d", "private")]):
            self.tick()
            self.admission.register_investor(
                request_id=f"inv-{index}", actor_id="op-1", investor_id=investor_id,
                name=investor_id, ownership_kind=kind)
        self.tick()
        self.admission.link_investor_relation(
            request_id="rel-1", actor_id="op-1", left_id="inv-a", right_id="inv-d",
            relation_type="同一实际控制人")
        for index, investor_id in enumerate(["inv-a", "inv-b", "inv-d"]):
            self.tick()
            self.admission.record_evidence(
                request_id=f"ev-{index}", actor_id="op-1",
                evidence_id=f"lic-{investor_id}", investor_id=investor_id,
                evidence_type="business_license", document_ref=f"d/{investor_id}",
                content_hash=f"hash-{investor_id}", valid_from="2026-01-01",
                valid_until="2027-12-31")
            self.tick()
            self.admission.verify_evidence(
                request_id=f"vf-{index}", actor_id="rev-1",
                evidence_id=f"lic-{investor_id}", verified=True)
        for index, (investor_id, amount) in enumerate(
                [("inv-a", 100.0), ("inv-b", 100.0), ("inv-d", 100.0)]):
            self.tick()
            self.admission.register_guarantee(
                request_id=f"gua-{index}", actor_id="op-1",
                guarantee_id=f"bond-{investor_id}", investor_id=investor_id,
                guarantee_type="performance_bond", amount=amount)
        for index, (investor_id, private_amount) in enumerate(
                [("inv-a", 600.0), ("inv-b", 360.0), ("inv-d", 600.0)]):
            self.tick()
            self.admission.submit_capital_plan(
                request_id=f"plan-{index}", actor_id="op-1",
                plan_id=f"plan-{investor_id}", project_id="ring",
                investor_id=investor_id, total_amount=600.0,
                private_amount=private_amount,
                tranches=[{"label": "一期", "amount": 600.0,
                           "scheduled_at": "2026-12-01"}])

    def _bid(self, request_id, review_id, applicant, *, assets, policy_ok=True,
             consortium=None, plan=None, guarantee=None):
        consortium = consortium or [
            {"investor_id": applicant, "shares": 0.7, "controller": True},
            {"investor_id": "inv-c", "shares": 0.3}]
        self.tick()
        return self.admission.propose_review(
            request_id=request_id, actor_id="op-1", review_id=review_id,
            project_id="ring", task_type="qualification", applicant_id=applicant,
            proposal={"stage_label": "main", "consortium": consortium,
                      "evidence_ids": [f"lic-{applicant}"],
                      "guarantee_ids": [guarantee or f"bond-{applicant}"],
                      "assets": assets,
                      "capital_plan_id": plan or f"plan-{applicant}"})

    def _sign(self, review_id, prefix, decision="approve", *, reviewer="rev-1",
              admin="admin-1"):
        self.tick()
        self.admission.record_approval(
            request_id=f"{prefix}-r", actor_id=reviewer, review_id=review_id,
            decision=decision)
        self.tick()
        return self.admission.record_approval(
            request_id=f"{prefix}-a", actor_id=admin, review_id=review_id,
            decision=decision)

    # -------------------------------------------------------------- 幂等与权限

    def test_idempotent_replay(self):
        first = self._bid("bid-a1", "q-a", "inv-a", assets=["asset-1"])
        second = self._bid("bid-a1", "q-a", "inv-a", assets=["asset-1"])
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)

    def test_auditor_cannot_propose(self):
        with self.assertRaises(PermissionDenied):
            self.admission.publish_policy(
                request_id="x1", actor_id="au-1", policy_id="rules2",
                thresholds={"min_private_ratio": 0.1})

    def test_proposer_cannot_sign(self):
        self._bid("bid-a1", "q-a", "inv-a", assets=["asset-1"])
        with self.assertRaises(PermissionDenied):
            self.admission.record_approval(
                request_id="self-sign", actor_id="op-1", review_id="q-a",
                decision="approve")

    def test_wrong_signoff_order_rejected(self):
        self._bid("bid-a1", "q-a", "inv-a", assets=["asset-1"])
        with self.assertRaises(PermissionDenied):
            self.admission.record_approval(
                request_id="admin-first", actor_id="admin-1", review_id="q-a",
                decision="approve")

    # -------------------------------------------------------------- 互斥与竞争

    def test_related_investors_cannot_bid_twice(self):
        self._bid("bid-a1", "q-a", "inv-a", assets=["asset-1"])
        with self.assertRaises(ConflictError):
            self._bid("bid-d1", "q-d", "inv-d", assets=["asset-2"])

    def test_same_asset_cannot_be_pledged_across_projects(self):
        self.tick()
        self.admission.publish_project_version(
            request_id="prj-v2", actor_id="admin-1", project_id="ring-2",
            name="二环线", total_investment=500.0, private_target_ratio=0.5,
            assets=["asset-9"])
        self._bid("bid-a1", "q-a", "inv-a", assets=["asset-1"])
        with self.assertRaises(ConflictError):
            self._bid("bid-b9", "q-b9", "inv-b", assets=["asset-1"])

    def test_only_one_competing_bid_takes_effect(self):
        self._bid("bid-a1", "q-a", "inv-a", assets=["asset-1"])
        self._bid("bid-b1", "q-b", "inv-b", assets=["asset-2"])
        self._sign("q-a", "sa")
        winner = self.admission.get_review("q-a")
        loser = self.admission.get_review("q-b")
        self.assertEqual(("approved", "awarded"), (winner.status, winner.outcome))
        self.assertEqual(("closed", "lost"), (loser.status, loser.outcome))
        self.assertEqual(0, len(self.admission.pending_queue()))

    def test_loser_assets_are_released(self):
        self._bid("bid-a1", "q-a", "inv-a", assets=["asset-1"])
        self._bid("bid-b1", "q-b", "inv-b", assets=["asset-2"])
        self._sign("q-a", "sa")
        self.assertEqual("released", self.admission.snapshot("ring")["assets"][1]["status"])

    def test_reject_releases_reserved_assets(self):
        self._bid("bid-a1", "q-a", "inv-a", assets=["asset-1"])
        self.tick()
        self.admission.record_approval(
            request_id="sj-r", actor_id="rev-1", review_id="q-a",
            decision="reject", comment="证据不足")
        review = self.admission.get_review("q-a")
        self.assertEqual("rejected", review.status)
        statuses = {item["asset_key"]: item["status"]
                    for item in self.admission.snapshot("ring")["assets"]}
        self.assertEqual("released", statuses["asset-1"])

    # -------------------------------------------------------------- 会签顺序

    def test_signoff_must_follow_fifo(self):
        self._bid("bid-a1", "q-a", "inv-a", assets=["asset-1"])
        self._bid("bid-b1", "q-b", "inv-b", assets=["asset-2"])
        with self.assertRaises(ConflictError):
            self.tick()
            self.admission.record_approval(
                request_id="out-of-order", actor_id="rev-1", review_id="q-b",
                decision="approve")

    def test_pending_queue_keeps_order_after_restart(self):
        self._bid("bid-a1", "q-a", "inv-a", assets=["asset-1"])
        self._bid("bid-b1", "q-b", "inv-b", assets=["asset-2"])
        restarted = AdmissionService(self.database, self.clock)
        queue = [item["review_id"] for item in restarted.pending_queue()]
        self.assertEqual(["q-a", "q-b"], queue)

    # -------------------------------------------------------------- 规则与版本

    def test_policy_revision_does_not_change_sealed_agreement(self):
        self._bid("bid-a1", "q-a", "inv-a", assets=["asset-1"])
        self._sign("q-a", "sa")
        sealed = self.admission.get_review("q-a").sealed_agreement
        before = sealed["agreement_hash"]
        self.tick()
        self.admission.publish_policy(
            request_id="pol-v2", actor_id="admin-1", policy_id="rules",
            thresholds={"min_private_ratio": 0.95, "min_guarantee_ratio": 0.5,
                        "required_evidence_types": ["business_license"],
                        "max_controller_share": 0.4})
        after = self.admission.get_review("q-a").sealed_agreement
        self.assertEqual(before, after["agreement_hash"])
        self.assertEqual(1, after["policy_version"]["version"])

    def test_snapshot_as_of_reconstructs_history(self):
        cutoff = self.clock.now().isoformat().replace("+00:00", "Z")
        self._bid("bid-a1", "q-a", "inv-a", assets=["asset-1"])
        snapshot = self.admission.snapshot("ring", as_of=cutoff)
        self.assertEqual([], snapshot["reviews"])
        current = self.admission.snapshot("ring")
        self.assertEqual(1, len(current["reviews"]))

    # -------------------------------------------------------------- 退出结清

    def _award_and_exit(self, exit_type="breach", paid_amount=100.0):
        self._bid("bid-a1", "q-a", "inv-a", assets=["asset-1"])
        self._sign("q-a", "sa")
        self.tick()
        self.admission.record_obligation(
            request_id="ob-paid", actor_id="op-1", obligation_id="ob-1",
            project_id="ring", investor_id="inv-a",
            obligation_type="capital_payment", description="已到资",
            amount=paid_amount, due_at="2026-12-01", status="paid")
        self.tick()
        self.admission.record_obligation(
            request_id="ob-pending", actor_id="op-1", obligation_id="ob-2",
            project_id="ring", investor_id="inv-a",
            obligation_type="construction", description="未完工程",
            amount=40.0, due_at="2027-06-01", status="in_progress")
        self.tick()
        self.admission.propose_review(
            request_id="exit-1", actor_id="op-1", review_id="x-1",
            project_id="ring", task_type="exit", applicant_id="inv-a",
            proposal={"stage_label": "main", "exit_type": exit_type,
                      "reason": "违约" if exit_type == "breach" else "协商"})
        self._sign("x-1", "sx")
        from transport_coordination.projection import build_projection
        projection = build_projection(
            self.admission.store.read_events(aggregate_type="review",
                                             aggregate_id="x-1"))
        return projection.reviews["x-1"]["settlement"]

    def test_breach_exit_settles_recourse_and_forfeits_bond(self):
        settlement = self._award_and_exit("breach")
        self.assertEqual(360.0, settlement["recourse_amount"])
        self.assertEqual(100.0, settlement["forfeited_amount"])
        self.assertEqual(0.0, settlement["refunded_amount"])
        self.assertEqual(["asset-1"], settlement["released_assets"])

    def test_default_exit_refunds_surplus_guarantee(self):
        settlement = self._award_and_exit("default", paid_amount=420.0)
        self.assertEqual(40.0, settlement["recourse_amount"])
        self.assertEqual(40.0, settlement["forfeited_amount"])
        self.assertEqual(60.0, settlement["refunded_amount"])

    # -------------------------------------------------------------- 结构性变更

    def test_member_change_and_control_change_require_signoff(self):
        self._bid("bid-a1", "q-a", "inv-a", assets=["asset-1"])
        self._sign("q-a", "sa")
        self.tick()
        self.admission.propose_review(
            request_id="mc-1", actor_id="op-1", review_id="m-1",
            project_id="ring", task_type="member_change", applicant_id="inv-a",
            proposal={"stage_label": "main",
                      "add_members": [{"investor_id": "inv-b", "shares": 0.3}],
                      "remove_members": ["inv-c"],
                      "reason": "引入民营股东并替换国企股东", "assets": []})
        self._sign("m-1", "sm")
        agreement = self.admission.get_review("m-1").sealed_agreement
        self.assertEqual(["inv-a", "inv-b"], sorted(c["investor_id"] for c in agreement["consortium"]))
        self.tick()
        self.admission.propose_review(
            request_id="cc-1", actor_id="op-1", review_id="c-1",
            project_id="ring", task_type="control_change", applicant_id="inv-b",
            proposal={"stage_label": "main", "old_controller": "inv-a",
                      "new_controller": "inv-b",
                      "holdings": {"inv-a": 0.4, "inv-b": 0.6}, "reason": "增持控股"})
        self._sign("c-1", "sc")
        control_agreement = self.admission.get_review("c-1").sealed_agreement
        self.assertEqual("inv-b", control_agreement["controller"])

    def test_member_change_rejects_member_with_open_obligations(self):
        self._bid("bid-a1", "q-a", "inv-a", assets=["asset-1"])
        self._sign("q-a", "sa")
        self.tick()
        self.admission.record_obligation(
            request_id="ob-open", actor_id="op-1", obligation_id="ob-open",
            project_id="ring", investor_id="inv-c",
            obligation_type="construction", description="未完工程",
            amount=10.0, due_at="2027-06-01", status="pending")
        with self.assertRaises(ValidationError):
            self.tick()
            self.admission.propose_review(
                request_id="mc-2", actor_id="op-1", review_id="m-2",
                project_id="ring", task_type="member_change", applicant_id="inv-a",
                proposal={"remove_members": ["inv-c"], "add_members": [],
                          "reason": "试图带走未完义务"})

    def test_control_change_uses_policy_frozen_at_agreement(self):
        self._bid("bid-a1", "q-a", "inv-a", assets=["asset-1"])
        self._sign("q-a", "sa")
        self.tick()
        self.admission.publish_policy(
            request_id="pol-v2", actor_id="admin-1", policy_id="rules",
            thresholds={"min_private_ratio": 0.95, "min_guarantee_ratio": 0.5,
                        "required_evidence_types": ["business_license"],
                        "max_controller_share": 0.5})
        self.tick()
        # inv-b 拟持股 0.6 超过新规上限 0.5，但协议冻结的是旧规 0.8，应受理并通过。
        self.admission.propose_review(
            request_id="mc-3", actor_id="op-1", review_id="m-3",
            project_id="ring", task_type="member_change", applicant_id="inv-a",
            proposal={"add_members": [{"investor_id": "inv-b", "shares": 0.3}],
                      "remove_members": ["inv-c"], "reason": "增持", "assets": []})
        self._sign("m-3", "sm3")
        self.tick()
        self.admission.propose_review(
            request_id="cc-3", actor_id="op-1", review_id="c-3",
            project_id="ring", task_type="control_change", applicant_id="inv-b",
            proposal={"old_controller": "inv-a", "new_controller": "inv-b",
                      "holdings": {"inv-a": 0.4, "inv-b": 0.6}, "reason": "控股"})
        self._sign("c-3", "sc3")
        self.assertEqual(
            0.8, self.admission.get_review("c-3").sealed_agreement[
                "policy_version"]["thresholds"]["max_controller_share"])


if __name__ == "__main__":
    unittest.main()
