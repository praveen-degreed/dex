import _env  # noqa: F401  (must come first)
import http.client, io, json, tempfile, threading, time, unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
from dex import config, ctl, dashboard, runtime, services, state
from dex.policy import Policy

DATA = Path(_env.DATA)
def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try: rc = ctl.main(list(argv))
        except SystemExit as e: rc = e.code
    return rc, out.getvalue(), err.getvalue()
def policy(): return json.loads((DATA / "policy.json").read_text())
def clean():
    for f in ("policy.json", "runtime.json", "usage.json", "audit.jsonl"): (DATA / f).unlink(missing_ok=True)

class Switches(unittest.TestCase):
    def setUp(self): clean()
    def test_pause_resume_disable_enable(self):
        rc, out, _ = run("pause"); self.assertEqual(rc, 0); self.assertTrue(policy()["paused"]); self.assertIn("paused: True", out)
        run("resume"); self.assertFalse(policy()["paused"])
        run("disable", "review", "question"); self.assertEqual(policy()["disabled"], ["question", "review"]); run("disable", "review"); self.assertEqual(policy()["disabled"], ["question", "review"], "no duplicates")
        run("enable", "review"); self.assertEqual(policy()["disabled"], ["question"]); run("enable", "question"); self.assertEqual(policy()["disabled"], [])
    def test_caps_set_change_and_remove(self):
        run("cap", "job-usd", "review", "4"); self.assertEqual(policy()["max_job_usd"], {"review": 4.0}); run("cap", "job-usd", "all", "0.5"); self.assertEqual(policy()["max_job_usd"], {"review": 0.5, "question": 0.5})
        run("cap", "job-usd", "review", "off"); self.assertEqual(policy()["max_job_usd"], {"question": 0.5}); run("cap", "job-usd", "question", "off"); self.assertNotIn("max_job_usd", policy())
        run("cap", "daily-usd", "10"); self.assertEqual(policy()["global"]["daily_usd"], 10.0); run("cap", "daily-usd", "off"); self.assertNotIn("daily_usd", policy()["global"])
        run("cap", "pr-files", "80"); self.assertEqual(policy()["max_pr_files"], 80); run("cap", "pr-files", "off"); self.assertNotIn("max_pr_files", policy())
        run("cap", "user-usd", "member", "2"); self.assertEqual(policy()["roles"]["member"]["daily_usd"], 2.0)
    def test_the_bot_obeys_what_dexctl_wrote(self):
        run("disable", "review"); run("cap", "job-usd", "question", "0.25"); run("cap", "pr-files", "50"); p = Policy(DATA, "UOWNER")
        self.assertFalse(p.acquire("U1", "review")[0]); self.assertEqual((p.max_job_usd("question"), p.max_pr_files()), (0.25, 50)); run("pause"); p.reload(); self.assertIn("paused", p.acquire("U1", "question")[1])
    def test_bad_input_changes_nothing(self):
        run("pause"); before = (DATA / "policy.json").read_text()
        for argv in (["disable", "nonsense"], ["cap", "job-usd", "review", "-3"], ["cap", "job-usd", "review", "lots"], ["cap", "job-usd", "bogus", "2"], ["cap", "daily-usd", "0"], ["cap", "pr-files", "abc"]):
            rc, _, err = run(*argv); self.assertEqual(rc, 2, argv); self.assertIn("error:", err)
        self.assertEqual((DATA / "policy.json").read_text(), before)
        for argv in (["frobnicate"], ["cap"], []): self.assertEqual(run(*argv)[0], 2, argv)
    def test_show_without_a_policy_file_says_unlimited(self):
        rc, out, _ = run("show"); self.assertEqual(rc, 0); self.assertIn("unlimited", out); self.assertIn("paused: False", out)

class Visibility(unittest.TestCase):
    def setUp(self):
        clean(); services.policy = Policy(DATA, "UOWNER")
        for d in (state.jobs, state.msg_usage): d.clear()
    def test_snapshot_lists_running_and_recent_with_tokens(self):
        state.new_job("r1", url="https://x/pull/1", sha="abcdef1234", repo="a/b", user="U1"); state.update_job("r1", status="running", started_at=time.time() - 90, steps=7, activity="reviewing")
        state.add_usage("r1", "m1", 100, 2000, 30); state.new_job("q1", kind="question", label="A question from <@U2>", url="", sha="", repo="", user="U2"); state.update_job("q1", status="done", finished_at=time.time(), started_at=time.time() - 3)
        services.policy.acquire("U1", "review"); services.policy.add_cost("U1", 1.25, {"fresh": 100, "cache_read": 2000, "output": 30})
        snap = runtime.snapshot(); self.assertEqual([r["id"] for r in snap["running"]], ["r1"]); r = snap["running"][0]
        self.assertEqual((r["kind"], r["user"], r["steps"], r["tokens"]["cache_read"], r["sha"]), ("review", "U1", 7, 2000, "abcdef1")); self.assertGreaterEqual(r["age_s"], 90)
        self.assertEqual([x["id"] for x in snap["recent"]], ["q1"]); self.assertEqual(snap["policy"]["users"]["U1"]["role"], "member"); self.assertAlmostEqual(snap["policy"]["global_usd"], 1.25)
        runtime.write_once(DATA / "runtime.json"); self.assertEqual(json.loads((DATA / "runtime.json").read_text())["running"][0]["id"], "r1"); self.assertFalse((DATA / "runtime.tmp").exists())
    def test_status_running_stale_and_missing(self):
        rc, out, _ = run("status"); self.assertEqual(rc, 1); self.assertIn("does not seem to be running", out)
        state.new_job("r1", url="https://x/pull/1", sha="abcdef1234", repo="a/b", user="U1"); state.update_job("r1", status="running", started_at=time.time() - 5, activity="reviewing"); services.policy = Policy(DATA, "UOWNER")
        (DATA / "policy.json").write_text(json.dumps({"paused": True, "disabled": ["review"], "max_pr_files": 80})); services.policy.reload(); runtime.write_once(DATA / "runtime.json")
        rc, out, _ = run("status"); self.assertEqual(rc, 0); self.assertIn("PAUSED", out); self.assertIn("switched off: review", out); self.assertIn("PR files <= 80", out); self.assertIn("https://x/pull/1", out); self.assertIn("reviewing", out)
        old = json.loads((DATA / "runtime.json").read_text()); old["updated_at"] -= 600; (DATA / "runtime.json").write_text(json.dumps(old))
        rc, out, _ = run("status"); self.assertEqual(rc, 1); self.assertIn("last seen 10m", out)
    def test_usage_and_top_from_the_audit_log(self):
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        rows = [
            {"ts": now, "user": "U1", "capability": "review", "decision": "done", "usd": 2.5, "pr": "https://x/pull/9", "tokens": {"fresh": 1000, "cache_read": 5000, "output": 500}},
            {"ts": now, "user": "U2", "capability": "question", "decision": "answered", "usd": 0.04, "tokens": {"fresh": 10, "cache_read": 20, "output": 5}},
            {"ts": "2000-01-01T00:00:00", "user": "U1", "capability": "review", "decision": "done", "usd": 99},
            {"ts": now, "user": "U3", "capability": "review", "decision": "denied"},
        ]
        (DATA / "audit.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
        rc, out, _ = run("usage", "--days", "7"); self.assertEqual(rc, 0); self.assertIn("review", out); self.assertIn("$   2.50", out); self.assertNotIn("99.00", out, "outside the window"); self.assertIn("6.5K tokens", out)
        rc, out, _ = run("top", "--n", "5", "--days", "7"); self.assertLess(out.index("2.50"), out.index("0.04")); self.assertIn("https://x/pull/9", out); self.assertNotIn("99.00", out)

class Dashboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        clean(); cls.server = dashboard.make_server(0); cls.port = cls.server.server_address[1]; threading.Thread(target=cls.server.serve_forever, daemon=True).start()
    @classmethod
    def tearDownClass(cls): cls.server.shutdown(); cls.server.server_close()
    def get(self, path, host=None, method="GET"):
        c = http.client.HTTPConnection("127.0.0.1", self.port); c.putrequest(method, path, skip_host=True); c.putheader("Host", host if host is not None else f"127.0.0.1:{self.port}"); c.endheaders(); r = c.getresponse(); body = r.read().decode(); h = dict(r.getheaders()); c.close(); return r.status, body, h
    def test_serves_the_page_and_state_on_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        st, body, h = self.get("/"); self.assertEqual(st, 200); self.assertIn("<title>Dex</title>", body); self.assertEqual(h["X-Content-Type-Options"], "nosniff"); self.assertIn("default-src 'none'", h["Content-Security-Policy"])
        st, body, h = self.get("/api/state"); self.assertEqual(st, 200); d = json.loads(body); self.assertTrue({"running", "by_capability", "top", "recent", "by_day"} <= set(d)); self.assertEqual(h["Content-Type"], "application/json")
        self.assertEqual(self.get("/nope")[0], 404)
    def test_refuses_foreign_hosts_and_any_change(self):
        for host in ("evil.example", f"evil.example:{self.port}", f"127.0.0.1:{self.port + 1}", "", f"127.0.0.1.evil.com:{self.port}"): self.assertEqual(self.get("/api/state", host=host)[0], 403, host)
        self.assertEqual(self.get("/api/state", host=f"localhost:{self.port}")[0], 200)
        self.assertEqual(self.get("/api/action", method="POST")[0], 403, "a change without the page's origin and token")
        for m in ("PUT", "DELETE", "PATCH", "OPTIONS"): self.assertEqual(self.get("/api/state", method=m)[0], 405, m)
    def test_the_page_pulls_nothing_external_and_never_uses_innerhtml(self):
        page = dashboard.PAGE; self.assertNotRegex(page, r"(?i)(src|href)\s*=\s*[\"']https?://"); self.assertNotIn("http://", page.replace("http://127.0.0.1", "").replace("http://www.w3.org/2000/svg", "")); self.assertNotIn("innerHTML", page); self.assertIn("textContent", page)

if __name__ == "__main__": unittest.main(verbosity=1)
