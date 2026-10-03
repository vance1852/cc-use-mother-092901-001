import unittest

from transport_coordination.api import route
from transport_coordination.participation import ParticipationService
from transport_coordination.service import DomainService
from transport_coordination.storage import Database


class ParticipationApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.base = DomainService(self.database)
        self.service = ParticipationService(self.database)
        self.base.register_organization(request_id="org", actor_id="bootstrap",
                                        organization_id="o1", name="推进办")
        self.base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                 display_name="管理员", role="admin", organization_id="o1")
        self.base.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                 display_name="经办", role="operator", organization_id="o1")
        self.base.register_actor(request_id="rev", actor_id="a1", new_actor_id="rv1",
                                 display_name="审查员", role="reviewer", organization_id="o1")

    def tearDown(self):
        self.database.close()

    def call(self, method, path, body=None, actor="op1"):
        return route(self.base, method, path, body or {}, {"X-Actor-Id": actor})

    def test_rule_project_investor_evidence_application_admission_flow(self):
        status, payload = self.call("POST", "/rule-versions", {
            "request_id": "r1",
            "rules": {"min_private_ratio": 0.3,
                      "required_evidence_kinds": ["business_license", "qualification"],
                      "guarantee_rates": {"bid": 0.1, "performance": 0.2}}}, actor="a1")
        self.assertEqual(201, status)
        self.assertEqual("rules-0001", payload["version_id"])

        self.assertEqual(201, self.call("POST", "/projects",
                                        {"request_id": "p1", "project_id": "proj1"})[0])
        self.assertEqual(201, self.call("POST", "/investors",
                                        {"request_id": "i1", "investor_id": "priv",
                                         "name": "民营甲", "kind": "private"})[0])
        self.assertEqual(201, self.call("POST", "/investors",
                                        {"request_id": "i2", "investor_id": "soe",
                                         "name": "国企丙", "kind": "state_owned"})[0])
        for eid, inv, kind in [("el", "priv", "business_license"),
                               ("eq", "priv", "qualification"),
                               ("sl", "soe", "business_license"),
                               ("sq", "soe", "qualification")]:
            self.assertEqual(201, self.call("POST", "/evidence", {
                "request_id": f"reg-{eid}", "evidence_id": eid, "investor_id": inv,
                "evidence_kind": kind, "document_hash": f"hash-{eid}-0000",
                "issued_at": "2026-01-01T00:00:00Z"})[0])
            self.assertEqual(201, self.call("POST", f"/evidence/{eid}/verify",
                                            {"request_id": f"ver-{eid}"}, actor="rv1")[0])

        status, payload = self.call("POST", "/projects/proj1/applications", {
            "request_id": "app1", "application_id": "app1", "lead_investor_id": "priv",
            "members": [{"investor_id": "priv", "member_role": "lead", "share": 0.4,
                         "entry_phase": 1},
                        {"investor_id": "soe", "member_role": "member", "share": 0.6,
                         "entry_phase": 1}],
            "contributions": {"priv": 4000.0, "soe": 6000.0},
            "evidence_ids": ["el", "eq", "sl", "sq"]})
        self.assertEqual(201, status)

        # 幂等回放
        status, replay = self.call("POST", "/projects/proj1/applications", {
            "request_id": "app1", "application_id": "app1", "lead_investor_id": "priv",
            "members": [{"investor_id": "priv", "member_role": "lead", "share": 0.4,
                         "entry_phase": 1},
                        {"investor_id": "soe", "member_role": "member", "share": 0.6,
                         "entry_phase": 1}],
            "contributions": {"priv": 4000.0, "soe": 6000.0},
            "evidence_ids": ["el", "eq", "sl", "sq"]})
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])

        for gid, inv, amount in [("g1", "priv", 400.0), ("g2", "soe", 600.0)]:
            self.assertEqual(201, self.call("POST", "/applications/app1/guarantees", {
                "request_id": gid, "guarantee_id": gid, "investor_id": inv,
                "guarantee_kind": "bid", "amount": amount})[0])

        self.assertEqual(201, self.call("POST", "/review-tasks", {
            "request_id": "t1", "kind": "admission",
            "payload": {"application_id": "app1"}})[0])
        self.assertEqual(201, self.call("POST", "/review-tasks/task-00001/sign", {
            "request_id": "s1", "decision": "approved"}, actor="rv1")[0])
        self.assertEqual(201, self.call("POST", "/review-tasks/task-00001/sign", {
            "request_id": "s2", "decision": "approved"}, actor="a1")[0])

        status, app = self.call("GET", "/applications/app1")
        self.assertEqual(200, status)
        self.assertEqual("selected", app["status"])

        status, agreement = self.call("GET", "/agreements/agr-app1")
        self.assertEqual(200, status)
        self.assertEqual("active", agreement["status"])
        self.assertEqual("rules-0001", agreement["rule_version_id"])

        status, snapshot = self.call("GET", "/projects/proj1/snapshot")
        self.assertEqual(200, status)
        self.assertEqual(1, len(snapshot["approval_chain"]))
        self.assertEqual(2, len(snapshot["approval_chain"][0]["signatures"]))

    def test_compare_candidates_endpoint(self):
        self.call("POST", "/rule-versions", {
            "request_id": "r1",
            "rules": {"min_private_ratio": 0.0, "required_evidence_kinds": [],
                      "guarantee_rates": {"bid": 0.0, "performance": 0.0}}}, actor="a1")
        self.call("POST", "/projects", {"request_id": "p1", "project_id": "proj1"})
        self.call("POST", "/investors", {"request_id": "i1", "investor_id": "priv",
                                         "name": "民营", "kind": "private"})
        status, payload = self.call("POST", "/projects/proj1/compare-candidates", {
            "candidates": [{"name": "方案甲",
                            "members": [{"investor_id": "priv", "share": 1.0,
                                         "is_private": True, "member_role": "lead",
                                         "entry_phase": 1}],
                            "contributions": {"priv": 1000.0}, "guarantees": {},
                            "evidence_ids": []}]})
        self.assertEqual(200, status)
        self.assertEqual("方案甲", payload["recommended"])

    def test_permission_denied_without_role(self):
        # operator 无权发布规则
        status, payload = self.call("POST", "/rule-versions", {
            "request_id": "r1",
            "rules": {"min_private_ratio": 0.3, "required_evidence_kinds": [],
                      "guarantee_rates": {}}}, actor="op1")
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_unknown_participation_route_404(self):
        status, payload = self.call("GET", "/nope")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
