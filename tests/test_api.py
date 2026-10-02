import unittest

from transport_coordination.api import route
from transport_coordination.service import DomainService
from transport_coordination.storage import Database


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(self.database)

    def tearDown(self):
        self.database.close()

    def test_health_is_available_without_actor(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_route_returns_404(self):
        status, payload = route(self.service, "GET", "/missing", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_invalid_json_shape_returns_400(self):
        status, payload = route(self.service, "POST", "/organizations", {"request_id": "x"},
                                {"X-Actor-Id": "bootstrap"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def _bootstrap_office(self):
        route(self.service, "POST", "/organizations",
              {"request_id": "org-1", "organization_id": "office", "name": "推进办"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "adm-1", "new_actor_id": "admin-1", "display_name": "管理员",
               "role": "admin", "organization_id": "office"},
              {"X-Actor-Id": "bootstrap"})

    def test_admission_policy_publish_and_get(self):
        self._bootstrap_office()
        status, payload = route(self.service, "POST", "/policies", {
            "request_id": "pol-1", "policy_id": "rules",
            "thresholds": {"min_private_ratio": 0.5}}, {"X-Actor-Id": "admin-1"})
        self.assertEqual(201, status)
        self.assertEqual("policy", payload["resource_type"])
        status, payload = route(self.service, "GET", "/policies?policy_id=rules", None,
                                {"X-Actor-Id": "admin-1"})
        self.assertEqual(200, status)
        self.assertEqual(1, payload["version"])

    def test_admission_route_requires_known_resource(self):
        self._bootstrap_office()
        status, payload = route(self.service, "GET", "/reviews?review_id=missing", None,
                                {"X-Actor-Id": "admin-1"})
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_review_queue_route(self):
        status, payload = route(self.service, "GET", "/review-queue", None)
        self.assertEqual(200, status)
        self.assertEqual([], payload["items"])


if __name__ == "__main__":
    unittest.main()
