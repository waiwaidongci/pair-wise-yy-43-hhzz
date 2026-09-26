import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service


def deploy_payload(segment_no="SB-01", vessel="海巡01",
                   start="2026-09-26T08:00:00Z", end="2026-09-26T09:00:00Z",
                   length=100.0, start_x=0.0, start_y=0.0, end_x=100.0, end_y=0.0):
    return {"segment_no": segment_no, "vessel": vessel, "start_time": start,
            "end_time": end, "length": length, "start_x": start_x, "start_y": start_y,
            "end_x": end_x, "end_y": end_y}


class BoomLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_deploy_registers_ledger_entry(self):
        dep = self.service.deploy_boom(deploy_payload(), "op1", "operations")
        self.assertEqual(dep["segment_no"], "SB-01")
        self.assertEqual(dep["vessel"], "海巡01")
        self.assertEqual(dep["start_time"], "2026-09-26T08:00:00+00:00")
        self.assertEqual(dep["end_time"], "2026-09-26T09:00:00+00:00")
        self.assertEqual(dep["length"], 100.0)
        self.assertEqual(dep["status"], "deployed")
        self.assertEqual(dep["join_status"], "complete")
        ledger = self.service.list_boom_deployments("viewer")
        self.assertEqual(len(ledger), 1)

    def test_duplicate_active_deployment_conflicts_and_names_occupant(self):
        first = self.service.deploy_boom(deploy_payload(), "op1", "operations")
        with self.assertRaises(ConflictError) as ctx:
            self.service.deploy_boom(deploy_payload(vessel="海巡02"), "op2", "operations")
        message = str(ctx.exception)
        self.assertIn(f"事件#{first['id']}", message)
        self.assertIn("海巡01", message)
        self.assertEqual(len(self.service.list_boom_deployments("viewer")), 1)

    def test_redeploy_allowed_after_recovery(self):
        dep = self.service.deploy_boom(deploy_payload(), "op1", "operations")
        self.service.recover_boom(dep["id"], {"recovered_length": 96.0}, "op1", "operations")
        again = self.service.deploy_boom(deploy_payload(vessel="海巡03"), "op2", "operations")
        self.assertEqual(again["vessel"], "海巡03")
        self.assertEqual(again["status"], "deployed")

    def test_gap_over_five_meters_marks_pending_redeploy(self):
        first = self.service.deploy_boom(deploy_payload("SB-01"), "op1", "operations")
        # 上一段终点(100,0)，本段起点(108,0)，端点间距8米>5米
        second = self.service.deploy_boom(
            deploy_payload("SB-02", start="2026-09-26T09:00:00Z",
                           end="2026-09-26T10:00:00Z", start_x=108.0, end_x=200.0),
            "op1", "operations")
        self.assertEqual(second["join_status"], "pending_redeploy")
        first_after = self.service.list_boom_deployments("viewer")[0]
        self.assertEqual(first_after["join_status"], "pending_redeploy")
        summary = self.service.boom_summary("viewer")
        self.assertEqual(summary["active"], 2)
        self.assertEqual(summary["complete"], 0)
        self.assertEqual(summary["pending_redeploy"], 2)

    def test_gap_within_five_meters_counts_complete(self):
        self.service.deploy_boom(deploy_payload("SB-01"), "op1", "operations")
        # 端点间距恰好5米，不超过阈值，计入完成
        second = self.service.deploy_boom(
            deploy_payload("SB-02", start="2026-09-26T09:00:00Z",
                           end="2026-09-26T10:00:00Z", start_x=105.0, end_x=200.0),
            "op1", "operations")
        self.assertEqual(second["join_status"], "complete")
        summary = self.service.boom_summary("viewer")
        self.assertEqual(summary["complete"], 2)
        self.assertEqual(summary["pending_redeploy"], 0)

    def test_excessive_loss_scraps_segment_and_blocks_redeploy(self):
        dep = self.service.deploy_boom(deploy_payload(length=100.0), "op1", "operations")
        # 损耗25米，超过原长两成(20米)，段报废
        result = self.service.recover_boom(dep["id"], {"recovered_length": 75.0},
                                           "op1", "operations")
        self.assertTrue(result["scrapped"])
        self.assertEqual(result["loss"], 25.0)
        self.assertIn("SB-01", self.service.boom_summary("viewer")["scrapped_segments"])
        with self.assertRaises(ConflictError) as ctx:
            self.service.deploy_boom(deploy_payload(), "op1", "operations")
        self.assertIn("报废", str(ctx.exception))

    def test_acceptable_loss_allows_redeploy(self):
        dep = self.service.deploy_boom(deploy_payload(length=100.0), "op1", "operations")
        # 损耗恰好两成，未超过，不报废
        result = self.service.recover_boom(dep["id"], {"recovered_length": 80.0},
                                           "op1", "operations")
        self.assertFalse(result["scrapped"])
        again = self.service.deploy_boom(deploy_payload(), "op1", "operations")
        self.assertEqual(again["status"], "deployed")

    def test_deploy_validation_and_permission(self):
        with self.assertRaises(PermissionDenied):
            self.service.deploy_boom(deploy_payload(), "x", "viewer")
        with self.assertRaises(ValidationError):
            self.service.deploy_boom(deploy_payload(end="2026-09-26T07:00:00Z"),
                                     "op1", "operations")
        with self.assertRaises(ValidationError):
            self.service.deploy_boom(deploy_payload(start="not-a-time"),
                                     "op1", "operations")
        with self.assertRaises(ValidationError):
            self.service.deploy_boom(deploy_payload(length=0), "op1", "operations")
        with self.assertRaises(ValidationError):
            self.service.deploy_boom(deploy_payload(vessel="  "), "op1", "operations")

    def test_recover_validation_and_permission(self):
        dep = self.service.deploy_boom(deploy_payload(), "op1", "operations")
        with self.assertRaises(PermissionDenied):
            self.service.recover_boom(dep["id"], {"recovered_length": 90.0}, "x", "viewer")
        with self.assertRaises(ValidationError):
            self.service.recover_boom(dep["id"], {"recovered_length": 120.0},
                                      "op1", "operations")
        with self.assertRaises(ValidationError):
            self.service.recover_boom(dep["id"], {"recovered_length": -1.0},
                                      "op1", "operations")
        self.service.recover_boom(dep["id"], {"recovered_length": 90.0}, "op1", "operations")
        with self.assertRaises(ConflictError):
            self.service.recover_boom(dep["id"], {"recovered_length": 90.0},
                                      "op1", "operations")

    def test_boom_audit_trail(self):
        dep = self.service.deploy_boom(deploy_payload(), "op1", "operations")
        self.service.recover_boom(dep["id"], {"recovered_length": 70.0}, "op1", "operations")
        events = self.service.audit("viewer")
        actions = [event["action"] for event in events]
        self.assertIn("boom_deploy", actions)
        self.assertIn("boom_recover", actions)
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
