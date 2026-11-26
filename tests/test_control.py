import _env  # noqa: F401  (must come first)
"""The dashboard's controls: the change guard on the page server, settings through dexctl's code, and the bot's
control inbox (stop, cancel, run, restart)."""
import http.client, json, shutil, threading, time, unittest
from pathlib import Path
from unittest import mock
from dex import config, control, dashboard, progress, runtime, services, state
from dex import slack_utils as su
from dex.policy import Policy

DATA = Path(_env.DATA)
OWNER = "UOWNER"


class FakeSlack:
    def __init__(self): self.posts = []
    def chat_postMessage(self, **kw): self.posts.append(kw); return {"channel": kw["channel"], "ts": f"{len(self.posts)}.000"}
    def reactions_add(self, **kw): pass
    def reactions_remove(self, **kw): pass


class FakeProc:
    def __init__(self): self.killed = False
    def poll(self): return None if not self.killed else -9
    def kill(self): self.killed = True


def reset():
    for f in ("policy.json", "audit.jsonl"): (DATA / f).unlink(missing_ok=True)
    shutil.rmtree(control.DIR, ignore_errors=True)
    with state.lock:
        for x in (state.jobs, state.threads, state.procs, state.qa_active): x.clear()
        state.stopped.clear(); state.cancelled.clear(); state.inflight.clear()
    with control._rlock:
        control._restart.update(requested_at=None, thread=None)
    services.policy = Policy(DATA, OWNER)
    su.client = FakeSlack()


def audit():
    p = DATA / "audit.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


class Page(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = dashboard.make_server(0, token="t0k3n-for-tests"); cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
    @classmethod
    def tearDownClass(cls): cls.server.shutdown(); cls.server.server_close()
    def setUp(self): reset()

    def req(self, method, path, body=None, origin="same", token="t0k3n-for-tests", ctype="application/json", extra=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.putrequest(method, path, skip_host=True); c.putheader("Host", f"127.0.0.1:{self.port}")
        if origin: c.putheader("Origin", f"http://127.0.0.1:{self.port}" if origin == "same" else origin)
        if token: c.putheader("X-Dex-Token", token)
        for k, v in (extra or {}).items(): c.putheader(k, v)
        data = json.dumps(body).encode() if body is not None else b""
        if body is not None: c.putheader("Content-Type", ctype); c.putheader("Content-Length", str(len(data)))
        c.endheaders(); c.send(data) if data else None
        r = c.getresponse(); out = r.read().decode(); c.close()
        try: out = json.loads(out)
        except ValueError: pass
        return r.status, out

    def test_page_carries_its_token(self):
        st, body = self.req("GET", "/", origin=None, token=None)
        self.assertEqual(st, 200); self.assertIn('content="t0k3n-for-tests"', body); self.assertNotIn("__DEX_TOKEN__", body)

    def test_a_change_needs_this_pages_origin_and_token(self):
        for kw in ({"token": None}, {"token": "wrong"}, {"origin": None}, {"origin": "https://evil.example"},
                   {"origin": f"http://127.0.0.1:{self.port + 1}"}, {"extra": {"Sec-Fetch-Site": "cross-site"}}):
            self.assertEqual(self.req("POST", "/api/action", {"action": "pause"}, **kw)[0], 403, kw)
        self.assertFalse((DATA / "policy.json").exists(), "nothing was written")
        self.assertEqual(self.req("POST", "/api/action", {"action": "pause"}, ctype="text/plain")[0], 415)
        self.assertEqual(self.req("GET", "/api/pr?url=x", token=None)[0], 403, "the PR preview runs gh: guarded too")

    def test_settings_go_through_dexctl_and_are_audited(self):
        st, r = self.req("POST", "/api/action", {"action": "pause"}); self.assertEqual((st, r["ok"]), (200, True))
        self.assertTrue(json.loads((DATA / "policy.json").read_text())["paused"])
        self.req("POST", "/api/action", {"action": "disable", "capability": "question"})
        self.req("POST", "/api/action", {"action": "cap", "what": "review", "value": "4"})
        self.req("POST", "/api/action", {"action": "cap", "what": "daily", "value": "40"})
        self.req("POST", "/api/action", {"action": "cap", "what": "pr_files", "value": "80"})
        self.req("POST", "/api/action", {"action": "account", "kind": "review", "profile": "subscription"})
        pol = json.loads((DATA / "policy.json").read_text())
        self.assertEqual((pol["disabled"], pol["max_job_usd"], pol["global"]["daily_usd"], pol["max_pr_files"], pol["claude_profile"]),
                         (["question"], {"review": 4.0}, 40.0, 80, {"review": "subscription"}))
        self.assertEqual(dashboard.settings()["accounts"]["review"], "subscription")
        rows = [a for a in audit() if a.get("source") == "page"]
        self.assertEqual(len(rows), 6); self.assertEqual(rows[1]["target"], "question")
        self.req("POST", "/api/action", {"action": "cap", "what": "review", "value": "off"})
        self.assertNotIn("max_job_usd", json.loads((DATA / "policy.json").read_text()))

    def test_bad_settings_change_nothing(self):
        self.req("POST", "/api/action", {"action": "pause"}); before = (DATA / "policy.json").read_text()
        for body in ({"action": "disable", "capability": "nonsense"}, {"action": "cap", "what": "review", "value": "-3"},
                     {"action": "cap", "what": "bogus", "value": "1"}, {"action": "account", "kind": "review", "profile": "nope"},
                     {"action": "account", "kind": "x", "profile": "foundry"}, {"action": "rm -rf"}):
            st, r = self.req("POST", "/api/action", body); self.assertIn(st, (400,), body); self.assertFalse(r["ok"], body)
        self.assertEqual((DATA / "policy.json").read_text(), before)

    def test_bot_actions_round_trip_through_the_inbox(self):
        bot = threading.Thread(target=lambda: [control.process_once() or time.sleep(0.1) for _ in range(40)], daemon=True); bot.start()
        st, r = self.req("POST", "/api/action", {"action": "stop", "job": "nope"})
        self.assertEqual(st, 200); self.assertFalse(r["ok"]); self.assertIn("no longer knows", r["message"])
        self.assertTrue(any(a.get("decision") == "page_stop" for a in audit()))

    def test_state_has_what_the_page_needs(self):
        st, d = self.req("GET", "/api/state", origin=None, token=None)
        self.assertTrue({"settings", "today", "names", "owner", "controls_ready"} <= set(d)); self.assertFalse(d["controls_ready"])


class Inbox(unittest.TestCase):
    def setUp(self): reset()

    def running_review(self, key="acme/web#1@abc", status="running"):
        state.new_job(key, url="https://github.com/acme/web/pull/1", sha="abc", repo="acme/web", user="U1", channel="C1", thread="9.1")
        state.update_job(key, status=status)
        with state.lock: state.threads[("C1", "9.1")] = key
        return key

    def test_stop_kills_the_running_claude_and_says_so_in_the_thread(self):
        key = self.running_review(); proc = FakeProc()
        with state.lock: state.procs[("C1", "9.1")] = proc
        cid = control.submit("stop", {"job": key}); control.process_once()
        self.assertTrue(proc.killed); self.assertIn(("C1", "9.1"), state.stopped)
        self.assertEqual(control.result(cid)["ok"], True)
        self.assertTrue(any("Dex page" in p["text"] for p in su.client.posts))

    def test_cancel_a_queued_review(self):
        key = self.running_review(status="queued")
        cid = control.submit("stop", {"job": key}); control.process_once()
        self.assertIn(key, state.cancelled); self.assertIn("had not started", control.result(cid)["message"])

    def test_a_review_still_getting_ready_is_not_killed_silently(self):
        key = self.running_review()  # no process yet: fetching the PR
        cid = control.submit("stop", {"job": key}); control.process_once()
        self.assertFalse(control.result(cid)["ok"]); self.assertIn("try again", control.result(cid)["message"])

    def test_old_and_garbage_commands_are_dropped(self):
        key = self.running_review(); proc = FakeProc()
        with state.lock: state.procs[("C1", "9.1")] = proc
        cid = control.submit("stop", {"job": key})
        control.process_once(now=time.time() + control.MAX_AGE_S + 5)
        self.assertFalse(proc.killed); self.assertIn("Expired", control.result(cid)["message"])
        (control.INBOX / "zzzzzzzzzz.json").write_text("not json"); (control.INBOX / "aaaaaaaaaa.json").write_text(json.dumps({"id": "other", "action": "stop"}))
        self.assertEqual(control.process_once(), 2); self.assertEqual(list(control.INBOX.glob("*.json")), [])
        self.assertRaises(ValueError, control.submit, "rm")

    def test_run_posts_a_thread_and_starts_the_review_there(self):
        from dex import reviews
        def fake_start(ch, ts, msg_ts, user, m, force):
            self.assertEqual((user, force, m.group(2)), (OWNER, True, "7"))
            with state.lock: state.threads[(ch, ts)] = "acme/web#7@x"
        with mock.patch.object(reviews, "start_review", side_effect=fake_start):
            cid = control.submit("run", {"url": "https://github.com/acme/web/pull/7", "force": True}); control.process_once()
        r = control.result(cid); self.assertTrue(r["ok"]); self.assertEqual(r["job"], "acme/web#7@x")
        self.assertEqual(su.client.posts[0]["channel"], OWNER)
        with mock.patch.object(reviews, "start_review") as start:
            cid = control.submit("run", {"url": "https://example.com/x"}); control.process_once()
            start.assert_not_called(); self.assertFalse(control.result(cid)["ok"])

    def test_restart_waits_for_running_work_and_can_be_cancelled(self):
        key = self.running_review()
        real_sleep = time.sleep
        with mock.patch.object(control.os, "execv") as ex, mock.patch.object(control.time, "sleep", lambda s: real_sleep(0.02)):
            ok, msg = control.do_restart({}); self.assertTrue(ok); self.assertIn("after 1 running job", msg)
            real_sleep(0.15); ex.assert_not_called(); self.assertTrue(control.status()["restart_pending"])
            self.assertTrue(control.do_cancel_restart({})[0]); self.assertFalse(control.status()["restart_pending"])
            real_sleep(0.1); ex.assert_not_called()
            state.update_job(key, status="done")
            ex.side_effect = lambda *a: control._restart.update(requested_at=None)
            control.do_restart({}); real_sleep(0.2); ex.assert_called_once()


class LiveDetail(unittest.TestCase):
    def setUp(self): reset()

    def test_tool_calls_and_agents_are_tracked_for_the_page(self):
        key = "acme/web#2@abc"; state.new_job(key, url="u", sha="abc", repo="r", user="U1", channel="C", thread="1")
        ev = lambda **e: json.dumps(e)
        progress.track(key, ev(type="assistant", message={"id": "m1", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "gh pr diff 2"}}]}))
        progress.track(key, ev(type="assistant", message={"id": "m2", "content": [{"type": "tool_use", "id": "a1", "name": "Agent", "input": {"subagent_type": "correctness", "description": "check logic"}}]}))
        progress.track(key, ev(type="assistant", parent_tool_use_id="a1", message={"id": "m3", "content": [{"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "/x.ts"}}]}))
        progress.track(key, ev(type="user", message={"content": [{"type": "tool_result", "tool_use_id": "a1"}]}))
        snap = runtime.snapshot(); row = next(r for r in snap["running"] if r["id"] == key)
        self.assertEqual([(l["tool"], l["main"]) for l in row["log"]], [("Bash", True), ("Agent", True), ("Read", False)])
        self.assertEqual(row["log"][0]["detail"], "gh pr diff 2")
        self.assertEqual([(a["name"], a["status"]) for a in row["agents"]], [("check logic", "done")])
        self.assertEqual((row["channel"], row["thread"]), ("C", "1")); self.assertIn("control", snap)


if __name__ == "__main__": unittest.main(verbosity=1)
