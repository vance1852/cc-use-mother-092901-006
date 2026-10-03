import unittest
from datetime import datetime, timezone

from transport_coordination.api import route
from transport_coordination.clock import FixedClock
from transport_coordination.renewal import RenewalService
from transport_coordination.service import DomainService
from transport_coordination.storage import Database

from test_renewal import BASE_TIME, bootstrap, make_phases


class RenewalApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(BASE_TIME)
        self.base = DomainService(self.database, clock)
        self.renewal = RenewalService(self.database, clock)
        bootstrap(self.base, self.renewal)

    def tearDown(self):
        self.database.close()

    def _call(self, method, path, body=None, actor=""):
        return route(self.base, method, path, body or {},
                     {"X-Actor-Id": actor}, renewal=self.renewal)

    def test_unknown_renewal_route_returns_404(self):
        status, payload = self._call("GET", "/renewal/unknown")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_missing_field_returns_400(self):
        status, payload = self._call("POST", "/renewal/plans",
                                     {"request_id": "x"}, actor="p1")
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_route_without_renewal_service_falls_through(self):
        status, _ = route(self.base, "GET", "/renewal/plans", None)
        self.assertEqual(404, status)

    def test_lock_conflict_response_carries_explanation(self):
        phases = make_phases(with_second=True)
        self._call("POST", "/renewal/plans", {
            "request_id": "req-1", "site_id": "s1", "plan_id": "plan-1",
            "facility_id": "f3", "title": "计划一", "phases": phases}, actor="p1")
        for party, actor in [("operations", "ops1"), ("construction", "con1"),
                             ("local_manager", "loc1")]:
            self._call("POST", "/renewal/plans/plan-1/approvals",
                       {"request_id": f"ap-1-{party}", "party": party}, actor=actor)
        self._call("POST", "/renewal/plans", {
            "request_id": "req-2", "site_id": "s1", "plan_id": "plan-2",
            "facility_id": "f3", "title": "计划二",
            "phases": make_phases(start="2026-10-15T00:00:00Z",
                                  end="2026-10-25T00:00:00Z", route_cap=3000)},
            actor="p1")
        for party, actor in [("operations", "ops1"), ("construction", "con1"),
                             ("local_manager", "loc1")]:
            self._call("POST", "/renewal/plans/plan-2/approvals",
                       {"request_id": f"ap-2-{party}", "party": party}, actor=actor)
        status, payload = self._call("POST", "/renewal/plans/plan-2/lock",
                                     {"request_id": "lock-2"}, actor="p1")
        self.assertEqual(409, status)
        self.assertEqual("conflict", payload["error"])
        self.assertTrue(payload["conflicts"])
        self.assertTrue(all(item["suggestion"] for item in payload["conflicts"]))

    def test_replay_endpoint_returns_events(self):
        self._call("POST", "/renewal/plans", {
            "request_id": "req-1", "site_id": "s1", "plan_id": "plan-1",
            "facility_id": "f3", "title": "计划一", "phases": make_phases()}, actor="p1")
        for party, actor in [("operations", "ops1"), ("construction", "con1"),
                             ("local_manager", "loc1")]:
            self._call("POST", "/renewal/plans/plan-1/approvals",
                       {"request_id": f"ap-{party}", "party": party}, actor=actor)
        status, payload = self._call("GET", "/renewal/plans/plan-1/replay")
        self.assertEqual(200, status)
        actions = [event["action"] for event in payload["events"]]
        self.assertIn("renewal_plan.drafted", actions)
        self.assertIn("renewal_plan.locked", actions)

    def test_calendar_endpoint(self):
        self._call("POST", "/renewal/plans", {
            "request_id": "req-1", "site_id": "s1", "plan_id": "plan-1",
            "facility_id": "f3", "title": "计划一", "phases": make_phases()}, actor="p1")
        status, payload = self._call("GET", "/renewal/calendar?site_id=s1")
        self.assertEqual(200, status)
        self.assertTrue(payload["entries"])

    def test_idempotent_replay_returns_same_payload(self):
        body = {"request_id": "req-1", "site_id": "s1", "plan_id": "plan-1",
                "facility_id": "f3", "title": "计划一", "phases": make_phases()}
        status_first, first = self._call("POST", "/renewal/plans", body, actor="p1")
        status_second, second = self._call("POST", "/renewal/plans", body, actor="p1")
        self.assertEqual(201, status_first)
        self.assertEqual(200, status_second)
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["plan_id"], second["plan_id"])


if __name__ == "__main__":
    unittest.main()
