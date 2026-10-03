import unittest

from transport_coordination.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["conflict_explained"])
        # 不可变记录：ph1 两次开工 + ph2 两次开工 + em1 一次
        self.assertEqual(5, result["outage_records"])
        # ph2 验收退回后支付保留，复工不重复付款
        self.assertEqual(4, result["payment_records"])
        self.assertEqual(550.0, result["payment_total"])
        self.assertEqual(4, result["revisions_locked"])


if __name__ == "__main__":
    unittest.main()
