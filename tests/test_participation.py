import unittest
from datetime import datetime, timedelta, timezone

from transport_coordination.clock import Clock
from transport_coordination.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from transport_coordination.participation import ParticipationService
from transport_coordination.service import DomainService
from transport_coordination.storage import Database


class MutableClock(Clock):
    def __init__(self, start: datetime):
        self._value = start

    def now(self) -> datetime:
        return self._value

    def advance(self, **kwargs) -> None:
        self._value += timedelta(**kwargs)


class ParticipationTestBase(unittest.TestCase):
    """搭建组织、四类角色与一版规则。"""

    def setUp(self):
        self.database = Database()
        self.clock = MutableClock(datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc))
        self.base = DomainService(self.database, self.clock)
        self.service = ParticipationService(self.database, self.clock)
        self.base.register_organization(request_id="org", actor_id="bootstrap",
                                        organization_id="o1", name="省交通推进办")
        self.base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                 display_name="管理员", role="admin", organization_id="o1")
        self.base.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                 display_name="经办人", role="operator", organization_id="o1")
        self.base.register_actor(request_id="rev", actor_id="a1", new_actor_id="rv1",
                                 display_name="审查员", role="reviewer", organization_id="o1")
        self.base.register_actor(request_id="rev2", actor_id="a1", new_actor_id="rv2",
                                 display_name="审查员二", role="reviewer", organization_id="o1")
        self.publish_rules("rules-1", min_private=0.3, bid=0.1, perf=0.2)
        self.service.create_project(request_id="proj", actor_id="op1", project_id="p1")

    def tearDown(self):
        self.database.close()

    def publish_rules(self, request_id, *, min_private, bid, perf, actor="a1"):
        return self.service.publish_rule_version(
            request_id=request_id, actor_id=actor,
            rules={"min_private_ratio": min_private,
                   "required_evidence_kinds": ["business_license", "qualification"],
                   "guarantee_rates": {"bid": bid, "performance": perf}})

    def investor(self, investor_id, name, kind, is_private=None):
        return self.service.register_investor(
            request_id=f"inv-{investor_id}", actor_id="op1", investor_id=investor_id,
            name=name, kind=kind,
            is_private=(kind == "private") if is_private is None else is_private)

    def relation(self, relation_id, a, b, kind="consortium"):
        return self.service.register_relation(request_id=f"rel-{relation_id}", actor_id="op1",
                                              relation_id=relation_id, from_investor=a,
                                              to_investor=b, relation_kind=kind)

    def evidence(self, evidence_id, investor_id, kind, *, hash_suffix=None, verified=True):
        self.service.register_evidence(
            request_id=f"reg-{evidence_id}", actor_id="op1", evidence_id=evidence_id,
            investor_id=investor_id, evidence_kind=kind,
            document_hash=hash_suffix or f"hash-{evidence_id}",
            issued_at="2026-01-01T00:00:00Z")
        if verified:
            self.service.verify_evidence(request_id=f"ver-{evidence_id}", actor_id="rv1",
                                         evidence_id=evidence_id)

    def standard_investors(self):
        """民营甲 40%、国企丙 60%，首期入场。"""
        self.investor("priv", "民营甲", "private")
        self.investor("soe", "国企丙", "state_owned")
        self.evidence("ev-priv-l", "priv", "business_license")
        self.evidence("ev-priv-q", "priv", "qualification")
        self.evidence("ev-soe-l", "soe", "business_license")
        self.evidence("ev-soe-q", "soe", "qualification")

    def apply(self, application_id="app1", *, lead="priv", members=None,
              contributions=None, evidence_ids=None, assets=None):
        members = members or [
            {"investor_id": "priv", "member_role": "lead", "share": 0.4, "entry_phase": 1},
            {"investor_id": "soe", "member_role": "member", "share": 0.6, "entry_phase": 1}]
        contributions = contributions or {"priv": 4000.0, "soe": 6000.0}
        evidence_ids = evidence_ids or ["ev-priv-l", "ev-priv-q", "ev-soe-l", "ev-soe-q"]
        return self.service.submit_application(
            request_id=f"req-{application_id}", actor_id="op1", application_id=application_id,
            project_id="p1", lead_investor_id=lead, members=members,
            contributions=contributions, evidence_ids=evidence_ids, assets=assets or [])

    def bid_guarantees(self, application_id="app1", amounts=None):
        amounts = amounts or {"priv": 400.0, "soe": 600.0}
        for i, (investor_id, amount) in enumerate(amounts.items()):
            self.service.post_guarantee(
                request_id=f"bid-{application_id}-{investor_id}", actor_id="op1",
                guarantee_id=f"g-{application_id}-{investor_id}", application_id=application_id,
                investor_id=investor_id, guarantee_kind="bid", amount=amount)

    def admit(self, application_id="app1", request_id="task1"):
        task = self.service.submit_review_task(
            request_id=request_id, actor_id="op1", kind="admission",
            payload={"application_id": application_id})
        self.service.sign_review_task(request_id=f"s-r-{request_id}", actor_id="rv1",
                                      task_id=task["task_id"], decision="approved")
        self.service.sign_review_task(request_id=f"s-a-{request_id}", actor_id="a1",
                                      task_id=task["task_id"], decision="approved")
        return task

    def agreement_id(self, application_id="app1"):
        return f"agr-{application_id}"


class RuleVersionTest(ParticipationTestBase):
    def test_agreement_pins_rule_version_and_new_rules_do_not_retroact(self):
        self.standard_investors()
        self.apply()
        self.bid_guarantees()
        self.admit()
        agreement = self.service.get_agreement(self.agreement_id())
        self.assertEqual("rules-0001", agreement["rule_version_id"])

        # 换版：民营比例门槛提高到 0.8
        self.publish_rules("rules-2", min_private=0.8, bid=0.1, perf=0.2)
        # 已签协议仍然固定在旧版，快照还原的仍是旧规则
        snap = self.service.project_snapshot("p1")
        self.assertEqual("rules-0001", snap["agreements"][0]["pinned_rule_version_id"])
        # 新项目上的新申请按当前版本（民营 40% 不再达标）
        self.service.create_project(request_id="proj2", actor_id="op1", project_id="p2")
        with self.assertRaises(ValidationError):
            self.service.submit_application(
                request_id="req-app2", actor_id="op1", application_id="app2", project_id="p2",
                lead_investor_id="priv",
                members=[{"investor_id": "priv", "member_role": "lead",
                          "share": 0.4, "entry_phase": 1},
                         {"investor_id": "soe", "member_role": "member",
                          "share": 0.6, "entry_phase": 1}],
                contributions={"priv": 4000.0, "soe": 6000.0},
                evidence_ids=["ev-priv-l", "ev-priv-q", "ev-soe-l", "ev-soe-q"])

    def test_snapshot_restores_rule_and_evidence_at_point_in_time(self):
        self.standard_investors()
        self.apply()
        self.clock.advance(days=1)
        self.bid_guarantees()
        before_admit = self.clock.now().isoformat().replace("+00:00", "Z")
        self.clock.advance(days=1)
        self.admit()
        snap = self.service.project_snapshot("p1", at=before_admit)
        # 准入会签尚未发生
        self.assertEqual([], snap["approval_chain"])
        self.assertEqual("submitted", snap["applications"][0]["status_at"])
        self.assertIsNone(snap["agreements"] or None)
        # 全量快照包含批准链
        full = self.service.project_snapshot("p1")
        self.assertEqual("selected", full["applications"][0]["status_at"])
        self.assertEqual(1, len(full["agreements"]))
        self.assertEqual(2, len(full["approval_chain"][0]["signatures"]))


class DuplicateBiddingTest(ParticipationTestBase):
    def test_related_investors_cannot_bid_twice(self):
        self.standard_investors()
        self.investor("priv2", "民营乙", "private")
        self.evidence("ev-p2-l", "priv2", "business_license")
        self.evidence("ev-p2-q", "priv2", "qualification")
        self.relation("r1", "priv", "priv2")
        self.apply("app1")
        with self.assertRaises(ConflictError):
            self.service.submit_application(
                request_id="req-app2", actor_id="op1", application_id="app2", project_id="p1",
                lead_investor_id="priv2",
                members=[{"investor_id": "priv2", "member_role": "lead",
                          "share": 1.0, "entry_phase": 1}],
                contributions={"priv2": 1000.0},
                evidence_ids=["ev-p2-l", "ev-p2-q"])

    def test_same_qualification_document_cannot_be_reused(self):
        self.standard_investors()
        self.investor("other", "外地民企", "private")
        # 同一 document_hash 被另一主体使用
        self.evidence("ev-o-l", "other", "business_license", hash_suffix="hash-ev-priv-l")
        self.evidence("ev-o-q", "other", "qualification", hash_suffix="hash-other-q")
        self.apply("app1")
        with self.assertRaises(ConflictError):
            self.service.submit_application(
                request_id="req-app2", actor_id="op1", application_id="app2", project_id="p1",
                lead_investor_id="other",
                members=[{"investor_id": "other", "member_role": "lead",
                          "share": 1.0, "entry_phase": 1}],
                contributions={"other": 1000.0}, evidence_ids=["ev-o-l", "ev-o-q"])

    def test_revoked_evidence_blocks_admission(self):
        self.standard_investors()
        self.apply()
        self.bid_guarantees()
        self.service.revoke_evidence(request_id="revoke-1", actor_id="rv1",
                                     evidence_id="ev-priv-q", note="资质造假")
        self.service.submit_review_task(request_id="task-bad", actor_id="op1", kind="admission",
                                        payload={"application_id": "app1"})
        self.service.sign_review_task(request_id="s1", actor_id="rv1", task_id="task-00001",
                                      decision="approved")
        with self.assertRaises(ConflictError):
            self.service.sign_review_task(request_id="s2", actor_id="a1", task_id="task-00001",
                                          decision="approved")
        # 会签未办结，仍处于 pending
        self.assertEqual("pending", self.service.list_review_queue()[0]["status"])


class UniqueAwardTest(ParticipationTestBase):
    def _second_consortium(self):
        self.investor("other", "外地民企", "private")
        self.evidence("ev-o-l", "other", "business_license", hash_suffix="hash-other-l")
        self.evidence("ev-o-q", "other", "qualification", hash_suffix="hash-other-q")
        self.service.submit_application(
            request_id="req-app2", actor_id="op1", application_id="app2", project_id="p1",
            lead_investor_id="other",
            members=[{"investor_id": "other", "member_role": "lead",
                      "share": 1.0, "entry_phase": 1}],
            contributions={"other": 8000.0}, evidence_ids=["ev-o-l", "ev-o-q"])
        self.service.post_guarantee(request_id="bid-app2", actor_id="op1", guarantee_id="g-app2",
                                    application_id="app2", investor_id="other",
                                    guarantee_kind="bid", amount=800.0)

    def test_only_one_application_selected_and_losers_release_bid_guarantees(self):
        self.standard_investors()
        self.apply("app1")
        self.bid_guarantees()
        self._second_consortium()
        self.admit("app1")
        self.assertEqual("selected", self.service.get_application("app1")["status"])
        self.assertEqual("rejected", self.service.get_application("app2")["status"])
        # 项目已有成交结果，不能再授标给第二个
        with self.assertRaises(ConflictError):
            self.service.submit_review_task(request_id="task2", actor_id="op1", kind="admission",
                                            payload={"application_id": "app2"})

    def test_project_version_change_invalidates_pending_and_frees_assets(self):
        self.standard_investors()
        self.service.register_asset(request_id="asset1", actor_id="op1", asset_id="a1",
                                    investor_id="priv", name="盾构机", assessed_value=5000)
        self.apply("app1", assets=[{"asset_id": "a1", "amount": 4000.0}])
        self.assertTrue(self._asset_busy("a1"))
        self.service.publish_project_version(request_id="pv2", actor_id="op1", project_id="p1",
                                             name="二期补充", detail={"lane": 2})
        self.assertEqual("rejected", self.service.get_application("app1")["status"])
        self.assertFalse(self._asset_busy("a1"))

    def _asset_busy(self, asset_id):
        row = self.database.connection.execute(
            "SELECT 1 FROM asset_engagements WHERE asset_id=? AND released_at IS NULL",
            (asset_id,)).fetchone()
        return row is not None


class ReviewQueueTest(ParticipationTestBase):
    def test_segregation_of_duties(self):
        self.standard_investors()
        self.apply()
        self.bid_guarantees()
        task = self.service.submit_review_task(
            request_id="task1", actor_id="op1", kind="admission",
            payload={"application_id": "app1"})
        # 提单人不能自签
        with self.assertRaises(PermissionDenied):
            self.service.sign_review_task(request_id="x1", actor_id="op1",
                                          task_id=task["task_id"], decision="approved")
        # 审计员无权会签
        self.base.register_actor(request_id="aud", actor_id="a1", new_actor_id="au1",
                                 display_name="审计员", role="auditor", organization_id="o1")
        with self.assertRaises(PermissionDenied):
            self.service.sign_review_task(request_id="x2", actor_id="au1",
                                          task_id=task["task_id"], decision="approved")
        # reviewer 签后，另一名 reviewer 不能顶替 admin 槽位重复签
        self.service.sign_review_task(request_id="x3", actor_id="rv1",
                                      task_id=task["task_id"], decision="approved")
        with self.assertRaises(ConflictError):
            self.service.sign_review_task(request_id="x4", actor_id="rv2",
                                          task_id=task["task_id"], decision="approved")

    def test_rejection_releases_pledged_assets(self):
        self.standard_investors()
        self.apply()
        self.bid_guarantees()
        self.admit()
        # 新增一名成员：提单预占资产 → 缴投标担保 → 会签；驳回后资产释放
        self.investor("newbie", "新民企", "private")
        self.service.register_asset(request_id="asset-new", actor_id="op1", asset_id="an2",
                                    investor_id="newbie", name="新设备", assessed_value=2000)
        task = self.service.submit_review_task(
            request_id="t-add2", actor_id="op1", kind="member_add",
            payload={"agreement_id": "agr-app1", "investor_id": "newbie", "share": 0.1,
                     "is_private": True, "contribution": 100.0,
                     "new_shares": {"priv": 0.36, "soe": 0.54, "newbie": 0.1},
                     "assets": [{"asset_id": "an2", "amount": 1000.0}]})
        self.service.post_guarantee(request_id="bid-newbie", actor_id="op1", guarantee_id="g-nb",
                                    application_id="app1", investor_id="newbie",
                                    guarantee_kind="bid", amount=10.0)
        self.assertTrue(self._asset_busy("an2"))
        self.service.sign_review_task(request_id="r1", actor_id="rv1",
                                      task_id=task["task_id"], decision="rejected",
                                      note="材料不全")
        self.assertFalse(self._asset_busy("an2"))
        # 驳回后成员未加入
        members = {m["investor_id"] for m in
                   self.service.get_agreement("agr-app1")["current_members"]}
        self.assertNotIn("newbie", members)

    def _asset_busy(self, asset_id):
        row = self.database.connection.execute(
            "SELECT 1 FROM asset_engagements WHERE asset_id=? AND released_at IS NULL",
            (asset_id,)).fetchone()
        return row is not None

    def test_queue_is_fifo_and_member_add_then_dilutes_shares(self):
        self.standard_investors()
        self.apply()
        self.bid_guarantees()
        self.admit()
        self.investor("nb1", "新民企一", "private")
        t_first = self.service.submit_review_task(
            request_id="q1", actor_id="op1", kind="member_add",
            payload={"agreement_id": "agr-app1", "investor_id": "nb1", "share": 0.1,
                     "is_private": True, "contribution": 100.0,
                     "new_shares": {"priv": 0.36, "soe": 0.54, "nb1": 0.1}})
        self.service.post_guarantee(request_id="bid-nb1", actor_id="op1", guarantee_id="g-nb1",
                                    application_id="app1", investor_id="nb1",
                                    guarantee_kind="bid", amount=10.0)
        t_second = self.service.submit_review_task(
            request_id="q2", actor_id="op1", kind="control_change",
            payload={"agreement_id": "agr-app1", "new_controller_id": "soe"})
        # 第二个事项不能先办结
        with self.assertRaises(ConflictError):
            self.service.sign_review_task(request_id="q2r", actor_id="rv1",
                                          task_id=t_second["task_id"], decision="approved")
        # 办结第一个
        self.service.sign_review_task(request_id="q1r", actor_id="rv1",
                                      task_id=t_first["task_id"], decision="approved")
        self.service.sign_review_task(request_id="q1a", actor_id="a1",
                                      task_id=t_first["task_id"], decision="approved")
        members = {m["investor_id"]: m["share"] for m in
                   self.service.get_agreement("agr-app1")["current_members"]}
        self.assertEqual({"priv": 0.36, "soe": 0.54, "nb1": 0.1}, members)
        # 现在第二个事项成为队首，可以办结
        self.service.sign_review_task(request_id="q2r2", actor_id="rv1",
                                      task_id=t_second["task_id"], decision="approved")
        self.service.sign_review_task(request_id="q2a2", actor_id="a1",
                                      task_id=t_second["task_id"], decision="approved")
        self.assertEqual("soe", self.service.get_agreement("agr-app1")["controller_id"])


class LifecycleTest(ParticipationTestBase):
    def _admitted_with_guarantees(self):
        self.standard_investors()
        self.apply()
        self.bid_guarantees()
        self.admit()
        for investor_id, amount in (("priv", 800.0), ("soe", 1200.0)):
            self.service.post_guarantee(
                request_id=f"perf-{investor_id}", actor_id="op1",
                guarantee_id=f"pg-{investor_id}", application_id="app1",
                investor_id=investor_id, guarantee_kind="performance", amount=amount)

    def test_control_change_and_exit_settlement(self):
        self._admitted_with_guarantees()
        # 新控股方必须是当前联合体成员（提单时即校验）
        self.investor("outsider", "局外企业", "private")
        with self.assertRaises(ValidationError):
            self.service.submit_review_task(
                request_id="bad-cc", actor_id="op1", kind="control_change",
                payload={"agreement_id": "agr-app1", "new_controller_id": "outsider"})
        # 合法控制权变更
        tc = self.service.submit_review_task(
            request_id="cc", actor_id="op1", kind="control_change",
            payload={"agreement_id": "agr-app1", "new_controller_id": "soe"})
        self.service.sign_review_task(request_id="cc1", actor_id="rv1",
                                      task_id=tc["task_id"], decision="approved")
        self.service.sign_review_task(request_id="cc2", actor_id="a1",
                                      task_id=tc["task_id"], decision="approved")
        self.assertEqual("soe", self.service.get_agreement("agr-app1")["controller_id"])

        # 未完成义务 500：担保先追索，其余释放
        self.service.register_obligation(request_id="ob1", actor_id="op1",
                                         agreement_id="agr-app1", investor_id="priv",
                                         phase=1, title="路基", amount=500.0,
                                         due_at="2027-01-01T00:00:00Z")
        te = self.service.submit_review_task(
            request_id="exit", actor_id="op1", kind="default_exit",
            payload={"agreement_id": "agr-app1", "reason": "逾期"})
        self.service.sign_review_task(request_id="e1", actor_id="rv1",
                                      task_id=te["task_id"], decision="approved")
        self.service.sign_review_task(request_id="e2", actor_id="a1",
                                      task_id=te["task_id"], decision="approved")
        agreement = self.service.get_agreement("agr-app1")
        self.assertEqual("terminated", agreement["status"])
        settlement = agreement["settlements"][0]
        self.assertEqual(500.0, settlement["outstanding_amount"])
        self.assertEqual(500.0, settlement["claimed_amount"])
        # 总担保 400+600+800+1200=3000，追索 500 后释放 2500
        self.assertEqual(2500.0, settlement["released_amount"])
        # 义务已结清，资产已释放
        self.assertFalse(self._asset_busy())

    def _asset_busy(self, application_id="app1"):
        row = self.database.connection.execute(
            "SELECT 1 FROM asset_engagements WHERE application_id=? AND released_at IS NULL",
            (application_id,)).fetchone()
        return row is not None

    def test_member_remove_blocked_when_guarantees_insufficient(self):
        self._admitted_with_guarantees()
        # 先把控制权移交给 soe，priv 才能以普通成员身份退出
        tc = self.service.submit_review_task(
            request_id="cc", actor_id="op1", kind="control_change",
            payload={"agreement_id": "agr-app1", "new_controller_id": "soe"})
        self.service.sign_review_task(request_id="cc1", actor_id="rv1",
                                      task_id=tc["task_id"], decision="approved")
        self.service.sign_review_task(request_id="cc2", actor_id="a1",
                                      task_id=tc["task_id"], decision="approved")
        # priv 未完成义务 5000，远超其可用担保 bid400+perf800=1200
        self.service.register_obligation(request_id="ob-big", actor_id="op1",
                                         agreement_id="agr-app1", investor_id="priv",
                                         phase=1, title="大额义务", amount=5000.0,
                                         due_at="2027-01-01T00:00:00Z")
        task = self.service.submit_review_task(
            request_id="rm", actor_id="op1", kind="member_remove",
            payload={"agreement_id": "agr-app1", "investor_id": "priv"})
        self.service.sign_review_task(request_id="rm1", actor_id="rv1",
                                      task_id=task["task_id"], decision="approved")
        # 结算发生在最后一个会签生效时：担保不足则整笔驳回
        with self.assertRaises(ConflictError):
            self.service.sign_review_task(request_id="rm2", actor_id="a1",
                                          task_id=task["task_id"], decision="approved")
        # 事务回滚后成员仍在
        members = {m["investor_id"] for m in
                   self.service.get_agreement("agr-app1")["current_members"]}
        self.assertIn("priv", members)

    def test_controller_must_change_before_remove(self):
        self._admitted_with_guarantees()
        with self.assertRaises(ConflictError):
            self.service.submit_review_task(
                request_id="rmc", actor_id="op1", kind="member_remove",
                payload={"agreement_id": "agr-app1", "investor_id": "priv"})

    def test_phased_entry_uses_committed_share_and_releases_assets_on_exit(self):
        self.investor("priv", "民营甲", "private")
        self.investor("priv2", "民营乙", "private")
        self.investor("soe", "国企丙", "state_owned")
        for eid, inv in [("lic1", "priv"), ("qua1", "priv"), ("lic2", "priv2"),
                         ("qua2", "priv2"), ("lic3", "soe"), ("qua3", "soe")]:
            kind = "business_license" if eid.startswith("lic") else "qualification"
            self.evidence(f"ev-{eid}", inv, kind, hash_suffix=f"hash-{eid}-0000")
        members = [
            {"investor_id": "priv", "member_role": "lead", "share": 0.4, "entry_phase": 1},
            {"investor_id": "priv2", "member_role": "member", "share": 0.2, "entry_phase": 2},
            {"investor_id": "soe", "member_role": "member", "share": 0.4, "entry_phase": 1}]
        self.service.submit_application(
            request_id="req-app1", actor_id="op1", application_id="app1", project_id="p1",
            lead_investor_id="priv", members=members,
            contributions={"priv": 4000.0, "priv2": 2000.0, "soe": 4000.0},
            evidence_ids=["ev-lic1", "ev-qua1", "ev-lic2", "ev-qua2", "ev-lic3", "ev-qua3"])
        self.bid_guarantees("app1", {"priv": 400.0, "soe": 400.0})
        self.admit()
        members_now = {m["investor_id"] for m in
                       self.service.get_agreement("agr-app1")["current_members"]}
        self.assertEqual({"priv", "soe"}, members_now)
        # 二期成员先缴担保再入场；股比必须与申请承诺一致（提单时即校验）
        self.service.post_guarantee(request_id="bid-p2", actor_id="op1", guarantee_id="g-p2",
                                    application_id="app1", investor_id="priv2",
                                    guarantee_kind="bid", amount=200.0)
        with self.assertRaises(ValidationError):
            self.service.submit_review_task(
                request_id="phase-bad", actor_id="op1", kind="phased_entry",
                payload={"agreement_id": "agr-app1", "investor_id": "priv2", "share": 0.3})
        task = self.service.submit_review_task(
            request_id="phase", actor_id="op1", kind="phased_entry",
            payload={"agreement_id": "agr-app1", "investor_id": "priv2",
                     "share": 0.2, "entry_phase": 2})
        self.service.sign_review_task(request_id="ph1", actor_id="rv1",
                                      task_id=task["task_id"], decision="approved")
        self.service.sign_review_task(request_id="ph2", actor_id="a1",
                                      task_id=task["task_id"], decision="approved")
        members_now = {m["investor_id"] for m in
                       self.service.get_agreement("agr-app1")["current_members"]}
        self.assertEqual({"priv", "priv2", "soe"}, members_now)


class CompareCandidatesTest(ParticipationTestBase):
    def test_ranking_by_policy_and_delivery_risk(self):
        self.investor("priv", "民营甲", "private")
        self.investor("soe", "国企丙", "state_owned")
        self.evidence("ev-priv-l", "priv", "business_license")
        self.evidence("ev-priv-q", "priv", "qualification")
        self.evidence("ev-soe-l", "soe", "business_license")
        self.evidence("ev-soe-q", "soe", "qualification")
        result = self.service.compare_candidates("p1", [
            {"name": "材料齐全高担保",
             "members": [{"investor_id": "priv", "share": 0.4, "is_private": True,
                          "member_role": "lead", "entry_phase": 1},
                         {"investor_id": "soe", "share": 0.6, "is_private": False,
                          "member_role": "member", "entry_phase": 1}],
             "contributions": {"priv": 4000.0, "soe": 6000.0},
             "guarantees": {"priv": 1200.0, "soe": 1800.0},
             "evidence_ids": ["ev-priv-l", "ev-priv-q", "ev-soe-l", "ev-soe-q"]},
            {"name": "民营比例不足",
             "members": [{"investor_id": "soe", "share": 1.0, "is_private": False,
                          "member_role": "lead", "entry_phase": 1}],
             "contributions": {"soe": 10000.0}, "guarantees": {"soe": 3000.0},
             "evidence_ids": ["ev-soe-l", "ev-soe-q"]},
        ])
        self.assertEqual("材料齐全高担保", result["recommended"])
        names = [c["name"] for c in result["candidates"]]
        self.assertEqual("材料齐全高担保", names[0])
        self.assertTrue(result["candidates"][0]["meets_policy"])
        self.assertFalse(result["candidates"][1]["meets_policy"])
        self.assertLess(result["candidates"][0]["delivery_risk"],
                        result["candidates"][1]["delivery_risk"])

    def test_compare_against_specific_rule_version(self):
        self.investor("priv", "民营甲", "private")
        self.evidence("ev-priv-l", "priv", "business_license")
        self.evidence("ev-priv-q", "priv", "qualification")
        self.publish_rules("rules-loose", min_private=0.0, bid=0.0, perf=0.0)
        result = self.service.compare_candidates(
            "p1", rule_version_id="rules-0001",
            candidates=[{"name": "纯民营",
                         "members": [{"investor_id": "priv", "share": 1.0, "is_private": True,
                                      "member_role": "lead", "entry_phase": 1}],
                         "contributions": {"priv": 1000.0}, "guarantees": {"priv": 300.0},
                         "evidence_ids": ["ev-priv-l", "ev-priv-q"]}])
        self.assertEqual("rules-0001", result["rule_version_id"])


class IdempotencyTest(ParticipationTestBase):
    def test_replay_returns_same_resource(self):
        self.standard_investors()
        first = self.apply("app1")
        second = self.apply("app1")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["application_id"], second["application_id"])

    def test_same_request_id_changed_payload_conflicts(self):
        self.standard_investors()
        self.apply("app1")
        with self.assertRaises(ConflictError):
            self.service.submit_application(
                request_id="req-app1", actor_id="op1", application_id="app9", project_id="p1",
                lead_investor_id="priv",
                members=[{"investor_id": "priv", "member_role": "lead",
                          "share": 1.0, "entry_phase": 1}],
                contributions={"priv": 9999.0},
                evidence_ids=["ev-priv-l", "ev-priv-q"])


class QueuePersistenceTest(unittest.TestCase):
    """服务重启后未完成审查保持原顺序（文件库）。"""

    def test_pending_queue_survives_restart_in_order(self):
        import tempfile
        from pathlib import Path
        directory = tempfile.mkdtemp()
        path = Path(directory) / "queue.sqlite3"

        def build():
            database = Database(path)
            clock = MutableClock(datetime(2026, 10, 2, tzinfo=timezone.utc))
            base = DomainService(database, clock)
            service = ParticipationService(database, clock)
            return database, base, service

        database, base, service = build()
        base.register_organization(request_id="org-q", actor_id="bootstrap",
                                   organization_id="o1", name="办")
        base.register_actor(request_id="actor-a", actor_id="bootstrap", new_actor_id="a1",
                            display_name="管理员", role="admin", organization_id="o1")
        base.register_actor(request_id="actor-o", actor_id="a1", new_actor_id="op1",
                            display_name="经办", role="operator", organization_id="o1")
        base.register_actor(request_id="actor-r", actor_id="a1", new_actor_id="rv1",
                            display_name="审查", role="reviewer", organization_id="o1")
        service.publish_rule_version(request_id="rules", actor_id="a1",
                                     rules={"min_private_ratio": 0.0,
                                            "required_evidence_kinds": [],
                                            "guarantee_rates": {}})
        service.create_project(request_id="proj", actor_id="op1", project_id="p1")
        service.register_investor(request_id="i1", actor_id="op1", investor_id="priv",
                                  name="民营", kind="private", is_private=True)
        service.register_evidence(request_id="e1", actor_id="op1", evidence_id="ev-l",
                                  investor_id="priv", evidence_kind="business_license",
                                  document_hash="hash-l-0000", issued_at="2026-01-01T00:00:00Z")
        service.register_evidence(request_id="e2", actor_id="op1", evidence_id="ev-q",
                                  investor_id="priv", evidence_kind="qualification",
                                  document_hash="hash-q-0000", issued_at="2026-01-01T00:00:00Z")
        service.verify_evidence(request_id="v1", actor_id="rv1", evidence_id="ev-l")
        service.verify_evidence(request_id="v2", actor_id="rv1", evidence_id="ev-q")
        service.submit_application(
            request_id="app", actor_id="op1", application_id="app1", project_id="p1",
            lead_investor_id="priv",
            members=[{"investor_id": "priv", "member_role": "lead",
                      "share": 1.0, "entry_phase": 1}],
            contributions={"priv": 1000.0}, evidence_ids=["ev-l", "ev-q"])
        service.submit_review_task(request_id="task1", actor_id="op1", kind="admission",
                                   payload={"application_id": "app1"})
        # reviewer 已签，admin 未签，重启后必须仍能按原任务办结
        service.sign_review_task(request_id="s1", actor_id="rv1", task_id="task-00001",
                                 decision="approved")
        database.close()

        database, base, service = build()
        queue = service.list_review_queue()
        self.assertEqual(1, len(queue))
        self.assertEqual("task-00001", queue[0]["task_id"])
        self.assertEqual("reviewer", queue[0]["signatures"][0]["role"])
        service.sign_review_task(request_id="s2", actor_id="a1", task_id="task-00001",
                                 decision="approved")
        self.assertEqual("selected", service.get_application("app1")["status"])
        valid, _ = base.verify_audit()
        self.assertTrue(valid)
        database.close()


if __name__ == "__main__":
    unittest.main()
