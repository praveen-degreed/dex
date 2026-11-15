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

class TokenUsage(unittest.TestCase):
    def test_tokens_accumulate_per_person_and_globally_and_show_in_the_summary(self):
        p, d = mk(); p.acquire("U1", "review"); p.add_cost("U1", 1.5, {"fresh": 90_000, "cache_read": 2_000_000, "output": 12_000}); p.add_cost("U1", 0.5, {"fresh": 10_000, "cache_read": 0, "output": 3_000}); p.add_cost("U2", 0.1)
        s = p.summary("U1"); self.assertIn("$2.00", s); self.assertIn("2.1M tokens (fresh 100.0K, cached 2.0M, out 15.0K)", s); self.assertNotIn("tokens", p.summary("U2"), "no tokens, nothing shown")
        saved = json.loads((d / "usage.json").read_text()); self.assertEqual(saved["tokens"], {"fresh": 100000, "cache_read": 2000000, "output": 15000}); self.assertEqual(saved["users"]["U1"]["tokens"]["output"], 15000)
        self.assertEqual(Policy(d, OWNER).usage["users"]["U1"]["tokens"]["fresh"], 100000, "persisted")
        old = json.loads((d / "usage.json").read_text()); del old["users"]["U1"]["tokens"]; (d / "usage.json").write_text(json.dumps(old)); q = Policy(d, OWNER); q.add_cost("U1", 0.1, {"fresh": 5}); self.assertEqual(q.usage["users"]["U1"]["tokens"]["fresh"], 5, "an older usage.json without tokens still works")

class Switches(unittest.TestCase):
    def test_paused_blocks_costly_work_for_everyone_but_not_status(self):
        p, _ = mk({"paused": True})
        for who in ("U1", OWNER):
            for cap in ("review", "question", "investigate", "data"):
                ok, why = p.acquire(who, cap); self.assertFalse(ok, (who, cap)); self.assertIn("paused", why)
            for cap in ("status", "stop", "usage", "help"): self.assertTrue(p.acquire(who, cap)[0], cap)
        self.assertEqual(p.summary("U1"), "<@U1> (member): no requests yet, $0.00. Resets in " + p.resets_in() + ".", "blocked requests are not counted")
    def test_disabled_capability_only_blocks_that_one(self):
        p, _ = mk({"disabled": ["review"]}); ok, why = p.acquire("U1", "review"); self.assertFalse(ok); self.assertIn("switched off", why); self.assertIn("`review`", why)
        self.assertTrue(p.acquire("U1", "question")[0]); self.assertEqual(p.disabled(), ["review"]); self.assertFalse(mk()[0].paused())
    def test_job_caps_and_pr_file_cap(self):
        p, _ = mk({"max_job_usd": {"review": 4, "question": 0.5}, "max_pr_files": 80}); self.assertEqual((p.max_job_usd("review"), p.max_job_usd("question"), p.max_job_usd("data")), (4.0, 0.5, None)); self.assertEqual(p.max_pr_files(), 80)
        p, _ = mk({"max_job_usd": 2}); self.assertEqual(p.max_job_usd("review"), 2.0, "a single number applies to every capability")
        p, _ = mk(); self.assertEqual((p.max_job_usd("review"), p.max_pr_files()), (None, None), "unlimited by default")
        for bad in ({"max_job_usd": "lots", "max_pr_files": "many"}, {"max_job_usd": {"review": 0}, "max_pr_files": 0}):
            p, _ = mk(bad); self.assertEqual((p.max_job_usd("review"), p.max_pr_files()), (None, None), bad)
    def test_edit_file_is_atomic_keeps_unknown_keys_and_is_picked_up(self):
        from dex.policy import edit_file
        p, d = mk({"users": {"U1": "viewer"}, "future_key": {"keep": True}})
        edit_file(d / "policy.json", lambda c: c.update(paused=True, disabled=["review"]))
        saved = json.loads((d / "policy.json").read_text()); self.assertEqual((saved["paused"], saved["disabled"], saved["future_key"], saved["users"]), (True, ["review"], {"keep": True}, {"U1": "viewer"})); self.assertFalse((d / "policy.json.tmp").exists())
        import os, time as t; os.utime(d / "policy.json", (t.time() + 5, t.time() + 5)); p.maybe_reload(); self.assertTrue(p.paused()); self.assertFalse(p.acquire("U1", "review")[0])
        (d / "policy.json").write_text("{bad"); edit_file(d / "policy.json", lambda c: c.update(paused=False)); self.assertEqual(json.loads((d / "policy.json").read_text()), {"paused": False}, "an unreadable file starts clean")
        edit_file(d / "new" if False else d / "fresh.json", lambda c: c.update(a=1)); self.assertEqual(json.loads((d / "fresh.json").read_text()), {"a": 1})

if __name__ == "__main__": unittest.main(verbosity=1)
