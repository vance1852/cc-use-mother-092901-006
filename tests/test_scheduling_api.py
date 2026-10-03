import unittest

from transport_coordination.api import route
from transport_coordination.service import DomainService
from transport_coordination.storage import Database


class SchedulingApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(self.database)
        self.call = lambda method, path, body=None, actor="adm": route(
            self.service, method, path, body or {}, {"X-Actor-Id": actor})
        self.call("POST", "/organizations", {"request_id": "org-1", "organization_id": "o1",
                                             "name": "中心"}, "bootstrap")
        self.call("POST", "/actors", {"request_id": "adm-1", "new_actor_id": "adm",
                                      "display_name": "管理员", "role": "admin",
                                      "organization_id": "o1"}, "bootstrap")
        for rid, aid, role in [("op-1", "op", "operator"), ("co-1", "co", "construction"),
                               ("lo-1", "lo", "locality")]:
            self.call("POST", "/actors", {"request_id": rid, "new_actor_id": aid,
                                          "display_name": role, "role": role,
                                          "organization_id": "o1"})
        self.call("POST", "/sites", {"request_id": "site-1", "site_id": "s1", "organization_id": "o1",
                                     "name": "节点", "timezone_name": "Asia/Shanghai"})
        self.call("POST", "/facilities", {"request_id": "fac-1", "facility_id": "brg", "site_id": "s1",
                                          "name": "桥", "facility_type": "bridge"})
        self.call("POST", "/corridors", {"request_id": "cor-1", "corridor_id": "detour", "name": "通道",
                                         "spare_capacity": 100})
        self.call("POST", "/crews", {"request_id": "crew-1", "crew_id": "crew-a", "name": "队",
                                     "qualifications": ["bridge"]})
        self.call("POST", "/materials", {"request_id": "mat-1", "material_id": "steel", "name": "钢材",
                                         "arrived_at": "2026-09-01T00:00:00Z"})
        self.call("POST", "/funds", {"request_id": "fund-1", "fund_id": "fund-1", "name": "资金",
                                     "amount": 1000, "valid_until": "2026-12-31T00:00:00Z"})

    def tearDown(self):
        self.database.close()

    def _phase(self, code="p1"):
        return {"phase_code": code, "title": code,
                "planned_start": "2026-10-05T08:00:00Z", "planned_end": "2026-10-05T18:00:00Z",
                "closure_scope": "full", "diverted_volume": 40, "corridor_id": "detour",
                "crew_id": "crew-a", "qualification": "bridge", "material_ids": ["steel"],
                "cost": 200, "kind": "renewal"}

    def test_full_flow_over_http(self):
        status, win = self.call("POST", "/renewal-windows",
                                {"request_id": "win-1", "facility_id": "brg", "fund_id": "fund-1",
                                 "title": "更新"}, "op")
        self.assertEqual(201, status)
        wid = win["window_id"]

        status, prop = self.call("POST", "/proposals",
                                 {"request_id": "pp-1", "window_id": wid, "phases": [self._phase()]},
                                 "op")
        self.assertEqual(201, status)
        self.assertEqual([], prop["conflicts"])
        pid = prop["proposal_id"]

        for rid, actor, party in [("sg-1", "op", "operations"), ("sg-2", "co", "construction"),
                                  ("sg-3", "lo", "locality")]:
            status, _ = self.call("POST", "/proposals/sign",
                                  {"request_id": rid, "proposal_id": pid, "party": party}, actor)
            self.assertEqual(201, status)
        status, locked = self.call("POST", "/proposals/confirm-lock",
                                   {"request_id": "lk-1", "proposal_id": pid}, "op")
        self.assertEqual(201, status)
        self.assertTrue(locked["locked"])

        status, _ = self.call("POST", "/phases/start",
                              {"request_id": "st-1", "window_id": wid, "phase_code": "p1"}, "co")
        self.assertEqual(201, status)
        status, done = self.call("POST", "/phases/complete",
                                 {"request_id": "cp-1", "window_id": wid, "phase_code": "p1"}, "co")
        self.assertEqual(201, status)
        self.assertEqual("completed", done["status"])

        status, reopened = self.call("POST", "/windows/reopen",
                                     {"request_id": "rp-1", "window_id": wid}, "op")
        self.assertEqual(201, status)
        self.assertEqual("reopened", reopened["status"])

        status, detail = self.call("GET", f"/windows/{wid}")
        self.assertEqual(200, status)
        self.assertEqual("reopened", detail["status"])
        status, timeline = self.call("GET", f"/windows/{wid}/timeline")
        self.assertEqual(200, status)
        self.assertTrue(timeline["events"])

    def test_missing_actor_is_rejected(self):
        status, payload = route(self.service, "POST", "/facilities",
                                {"request_id": "f-x", "facility_id": "fx", "site_id": "s1",
                                 "name": "x", "facility_type": "x"}, {"X-Actor-Id": ""})
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_calendar_and_recover_available(self):
        status, payload = self.call("GET", "/calendar")
        self.assertEqual(200, status)
        self.assertIn("leases", payload)
        status, payload = self.call("POST", "/recover")
        self.assertEqual(200, status)
        self.assertIn("active_windows", payload)

    def test_window_404_unknown_id(self):
        status, payload = self.call("GET", "/windows/win-nope")
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
