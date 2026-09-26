import tempfile, unittest
from pathlib import Path
from src import rules
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
def seg(no,boat,start_m,end_m,length=100):
    return {"segment_no":no,"boat":boat,"start_time":"2026-09-26T08:00:00Z","end_time":"2026-09-26T09:00:00Z","length_m":length,"start_m":start_m,"end_m":end_m}
class BoomTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
        self.item=self.service.create_item({"title":"boom incident","description":"boom ledger","severity":"major","quantity":10,"threshold":5},"creator","observer")
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def deploy(self,payload,item=None,role="operations"):
        return self.service.deploy_boom((item or self.item)["id"],payload,"op",role)
    def test_deploy_registers_ledger_fields(self):
        d=self.deploy(seg("A-01","海巡01",0,100))
        self.assertEqual(d["status"],"deployed"); self.assertEqual(d["segment_no"],"A-01"); self.assertEqual(d["boat"],"海巡01"); self.assertEqual(d["length_m"],100)
        self.assertEqual(d["start_time"],"2026-09-26T08:00:00+00:00")
    def test_duplicate_active_segment_conflict_names_occupying_event(self):
        self.deploy(seg("A-01","海巡01",0,100))
        with self.assertRaises(ConflictError) as ctx: self.deploy(seg("A-01","海巡02",0,100))
        message=str(ctx.exception); self.assertIn("A-01",message); self.assertIn("海巡01",message); self.assertIn(f"事件#{self.item['id']}",message)
        other=self.service.create_item({"title":"second incident","description":"other event","severity":"minor","quantity":1,"threshold":5},"creator","observer")
        with self.assertRaises(ConflictError) as ctx2: self.deploy(seg("A-01","海巡03",0,100),item=other)
        self.assertIn(f"事件#{self.item['id']}",str(ctx2.exception))
    def test_gap_over_5m_counts_as_needs_redeploy_not_complete(self):
        self.deploy(seg("A-01","海巡01",0,100)); self.deploy(seg("A-02","海巡02",106,206))
        summary=self.service.boom_summary(self.item["id"],"viewer")
        self.assertEqual(summary["complete"],[]); self.assertEqual(summary["needs_redeploy"],["A-01","A-02"])
        self.assertEqual(len(summary["gaps"]),1); self.assertAlmostEqual(summary["gaps"][0]["gap_m"],6.0)
        listed=self.service.list_boom(self.item["id"],"viewer")
        self.assertTrue(all(d["disposition"]=="needs_redeploy" for d in listed))
    def test_gap_exactly_5m_still_complete(self):
        self.deploy(seg("B-01","海巡01",0,100)); self.deploy(seg("B-02","海巡02",105,205))
        summary=self.service.boom_summary(self.item["id"],"viewer")
        self.assertEqual(summary["complete"],["B-01","B-02"]); self.assertEqual(summary["needs_redeploy"],[]); self.assertEqual(summary["gaps"],[])
    def test_recovery_allows_redeploy_and_records_recovered_length(self):
        d=self.deploy(seg("C-01","海巡01",0,100))
        rec=self.service.recover_boom(self.item["id"],d["id"],{"recovered_length_m":90},"op","operations")
        self.assertEqual(rec["status"],"recovered"); self.assertEqual(rec["recovered_length_m"],90)
        again=self.deploy(seg("C-01","海巡02",0,100)); self.assertEqual(again["status"],"deployed"); self.assertNotEqual(again["id"],d["id"])
    def test_loss_over_twenty_percent_scraps_segment_forever(self):
        d=self.deploy(seg("D-01","海巡01",0,100))
        rec=self.service.recover_boom(self.item["id"],d["id"],{"recovered_length_m":75},"op","operations")
        self.assertEqual(rec["status"],"scrapped")
        with self.assertRaises(ConflictError) as ctx: self.deploy(seg("D-01","海巡02",0,100))
        self.assertIn("报废",str(ctx.exception))
    def test_loss_exactly_twenty_percent_not_scrapped(self):
        d=self.deploy(seg("E-01","海巡01",0,100))
        rec=self.service.recover_boom(self.item["id"],d["id"],{"recovered_length_m":80},"op","operations")
        self.assertEqual(rec["status"],"recovered")
    def test_double_recovery_conflict(self):
        d=self.deploy(seg("F-01","海巡01",0,100))
        self.service.recover_boom(self.item["id"],d["id"],{"recovered_length_m":95},"op","operations")
        with self.assertRaises(ConflictError): self.service.recover_boom(self.item["id"],d["id"],{"recovered_length_m":95},"op","operations")
    def test_validation_and_permission(self):
        with self.assertRaises(PermissionDenied): self.deploy(seg("G-01","海巡01",0,100),role="viewer")
        bad=dict(seg("G-01","海巡01",0,100)); bad["end_time"]="2026-09-26T07:00:00Z"
        with self.assertRaises(ValidationError): self.deploy(bad)
        with self.assertRaises(ValidationError): self.deploy(seg("G-01","海巡01",0,100,length=0))
        with self.assertRaises(ValidationError): self.deploy(seg("G-01","海巡01",100,0))
        d=self.deploy(seg("G-02","海巡01",0,100))
        with self.assertRaises(ValidationError): self.service.recover_boom(self.item["id"],d["id"],{"recovered_length_m":120},"op","operations")
    def test_boom_audit_events_on_item_trail(self):
        d=self.deploy(seg("H-01","海巡01",0,100))
        self.service.recover_boom(self.item["id"],d["id"],{"recovered_length_m":70},"op","operations")
        actions=[e["action"] for e in self.service.audit("viewer",self.item["id"])]
        self.assertIn("boom_deploy",actions); self.assertIn("boom_recover",actions); self.assertTrue(self.repo.verify_audit_chain())
class BoomRulesTest(unittest.TestCase):
    def test_recovery_status_boundary(self):
        self.assertEqual(rules.recovery_status(100,80),"recovered"); self.assertEqual(rules.recovery_status(100,79.9),"scrapped")
    def test_joint_gaps_sorted_by_position(self):
        deployments=[{"id":2,"segment_no":"B","start_m":110.0,"end_m":200.0},{"id":1,"segment_no":"A","start_m":0.0,"end_m":100.0}]
        gaps=rules.joint_gaps(deployments)
        self.assertEqual(len(gaps),1); self.assertEqual(gaps[0]["left_segment"],"A"); self.assertEqual(gaps[0]["gap_m"],10.0)
if __name__=="__main__": unittest.main()
