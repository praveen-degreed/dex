import json, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dex.policy import Policy

OWNER = "UOWNER"

def mk(cfg=None):
    d = Path(tempfile.mkdtemp())
    if cfg is not None: (d / "policy.json").write_text(json.dumps(cfg))
    return Policy(d, OWNER), d

class PolicyTests(unittest.TestCase):
    def test_no_file_means_everything_allowed_and_unlimited(self):
        p, _ = mk()
        for cap in ("review", "question", "investigate", "data", "status", "stop", "usage", "help"):
            for user in (OWNER, "UANY"):
                for _ in range(50): self.assertEqual(p.acquire(user, cap), (True, ""))
        self.assertEqual(p.role_of(OWNER), "owner"); self.assertEqual(p.role_of("UANY"), "member")

    def test_usage_is_tracked_even_when_unlimited(self):
        p, d = mk()
        for _ in range(3): p.acquire("U1", "question")
        p.add_cost("U1", 0.25)
        self.assertIn("question 3", p.summary("U1")); self.assertIn("$0.25", p.summary("U1"))
        self.assertEqual(json.loads((d / "usage.json").read_text())["users"]["U1"]["counts"]["question"], 3)
        self.assertEqual(Policy(d, OWNER).usage["users"]["U1"]["counts"]["question"], 3, "persisted across restart")

    def test_count_limit_is_enforced_only_where_set(self):
        p, _ = mk({"roles": {"member": {"daily": {"question": 2}}}})
        self.assertTrue(p.acquire("U1", "question")[0]); self.assertTrue(p.acquire("U1", "question")[0])
        ok, why = p.acquire("U1", "question"); self.assertFalse(ok); self.assertIn("2", why); self.assertIn("resets in", why)
        self.assertTrue(p.acquire("U1", "review")[0], "other capabilities stay unlimited")
        self.assertTrue(p.acquire("U2", "question")[0], "limits are per person")
        self.assertTrue(all(p.acquire(OWNER, "question")[0] for _ in range(10)), "owner role has no limit set")

    def test_check_does_not_count(self):
        p, _ = mk({"roles": {"member": {"daily": {"question": 1}}}})
        for _ in range(5): self.assertTrue(p.check("U1", "question")[0])
        self.assertTrue(p.acquire("U1", "question")[0]); self.assertFalse(p.check("U1", "question")[0])

    def test_role_capability_list(self):
        p, _ = mk({"users": {"U9": "viewer"}, "roles": {"viewer": {"can": ["question", "status"]}}})
        self.assertTrue(p.acquire("U9", "question")[0]); self.assertTrue(p.check("U9", "status")[0])
        ok, why = p.acquire("U9", "review"); self.assertFalse(ok); self.assertIn("viewer", why)
        self.assertTrue(p.acquire("UOTHER", "review")[0], "default role has no list => allowed")

    def test_usd_budgets_per_user_and_global(self):
        p, _ = mk({"roles": {"member": {"daily_usd": 1.0}}, "global": {"daily_usd": 1.5}})
        p.add_cost("U1", 1.0); ok, why = p.acquire("U1", "question"); self.assertFalse(ok); self.assertIn("budget", why)
        p.add_cost("U2", 0.6); ok, why = p.acquire("U2", "question"); self.assertFalse(ok); self.assertIn("daily budget", why)
        self.assertTrue(p.check("U2", "status")[0], "status/help are never blocked by budgets")

    def test_router_call_cap(self):
        p, _ = mk({"global": {"daily_router_calls": 2}})
        self.assertEqual([p.router_call_allowed() for _ in range(3)], [True, True, False])
        p2, _ = mk(); self.assertTrue(all(p2.router_call_allowed() for _ in range(100)))

    def test_bad_or_missing_config_falls_back_to_permissive(self):
        d = Path(tempfile.mkdtemp()); (d / "policy.json").write_text("{not json")
        self.assertTrue(Policy(d, OWNER).acquire("U1", "review")[0])
        (d / "policy.json").write_text("[1,2]"); self.assertTrue(Policy(d, OWNER).acquire("U1", "review")[0])

    def test_day_rollover_resets_counts(self):
        p, _ = mk({"roles": {"member": {"daily": {"question": 1}}}})
        p.acquire("U1", "question"); self.assertFalse(p.check("U1", "question")[0])
        p.usage["date"] = "2000-01-01"; self.assertTrue(p.check("U1", "question")[0])

    def test_summary_for_owner_lists_everyone_and_audit_appends(self):
        p, d = mk(); p.acquire("U1", "review"); p.acquire("U2", "question"); p.add_cost("U2", 0.5)
        s = p.summary(OWNER, everyone=True); self.assertIn("<@U1>", s); self.assertIn("<@U2>", s); self.assertIn("total $0.50", s)
        p.audit(user="U1", capability="review", decision="allowed"); p.audit(user="U2", capability="question")
        lines = (d / "audit.jsonl").read_text().splitlines(); self.assertEqual(len(lines), 2); self.assertEqual(json.loads(lines[0])["user"], "U1")

if __name__ == "__main__": unittest.main(verbosity=1)
