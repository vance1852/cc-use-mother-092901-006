import unittest

from transport_coordination.renewal_acceptance import run


class RenewalAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["conflict_explained"])
        self.assertTrue(result["history_preserved"])
        self.assertTrue(result["restart_recovered"])
        self.assertEqual(["plan-a", "plan-b"], result["reopened"])
        self.assertIn("crew_double_booked", result["conflict_types"])
        self.assertIn("route_capacity_exceeded", result["conflict_types"])
        self.assertGreater(result["replayed_events"], 0)


if __name__ == "__main__":
    unittest.main()
