import _env  # noqa: F401  (must come first)
import json, os, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path
from unittest import mock
from dex import config, progress, qa, state, tools
from dex import slack_utils as su
from dex.claude_runner import Stopped, run_claude

def tu(name, **inp): return json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}})

class Progress(unittest.TestCase):
    def test_describe(self):
        d = lambda s: progress.describe(json.loads(s))
        self.assertEqual(d(tu("Bash", command="/x/fetch-pr.sh 1")), "fetching the PR")
        self.assertEqual(d(tu("Bash", command="/x/pr-worktree.sh create 1")), "creating a worktree at the PR head")
        self.assertEqual(d(tu("Bash", command="gh pr review 5 --approve")), "posting the review")
        self.assertEqual(d(tu("Bash", command="ls")), "running a command")
        self.assertEqual(d(tu("Agent", subagent_type="fe-logic-reviewer")), "running reviewer fe-logic-reviewer")
        self.assertEqual(d(tu("Read", file_path="a")), "reading the code")
        self.assertIsNone(d(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}})))
        self.assertIsNone(d(json.dumps({"type": "system"})))

    def test_track_status_and_overview(self):
        state.jobs.clear(); state.qa_active.clear()
        state.new_job("k", url="https://x/pull/1", sha="abcdef1234", repo="degreed/fe-workspace")
        self.assertIn("queued (0 ahead", progress.status_text("k"))
        state.update_job("k", status="running", started_at=time.time() - 65)
        progress.track("k", tu("Bash", command="triage.sh")); self.assertIsNone(progress.track("k", "not json"))
        progress.track(None, tu("Read", file_path="a"))  # no key: parses, updates nothing
        s = progress.status_text("k"); self.assertIn("planning the review", s); self.assertIn("1 steps", s)
        state.update_job("k", status="done", outcome="approved", link="L", finished_at=time.time())
        self.assertIn("approved", progress.status_text("k"))
        state.update_job("k", status="stopped"); self.assertIn("stopped", progress.status_text("k"))
        state.jobs.clear(); self.assertEqual(progress.overview_text(), "Nothing is running or queued.")
        state.qa_active[("C", "1")] = time.time(); self.assertIn("1 question(s)", progress.overview_text()); state.qa_active.clear()

class State(unittest.TestCase):
    def test_claim_release_dedupe_and_stop(self):
        self.assertTrue(state.claim("r#1@a", False)); self.assertFalse(state.claim("r#1@a", False)); self.assertTrue(state.claim("r#1@a", True))
        state.release("r#1@a"); self.assertTrue(state.claim("r#1@a", False))
        self.assertIn("r#1@a", json.loads(config.STATE.read_text()), "claims are persisted")
        self.assertFalse(state.handled_before("C", "1.0")); self.assertTrue(state.handled_before("C", "1.0")); self.assertFalse(state.handled_before("C", "2.0"))
        self.assertIsNone(state.stop_thread("C", "none"))
        state.jobs["q"] = {"status": "queued"}; state.threads[("C", "9")] = "q"
        self.assertEqual(state.stop_thread("C", "9"), "queued"); self.assertIn("q", state.cancelled); state.cancelled.clear()

class Runner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Path(tempfile.mkdtemp()); (cls.d / "fake.py").write_text(
            "import sys, json, time\nm = sys.argv[1]\n"
            "print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Bash','input':{'command':'triage.sh'}}]}}), flush=True)\n"
            "if m == 'slow': time.sleep(30)\nif m == 'fail': sys.exit(3)\n"
            "print(json.dumps({'type':'result','is_error': m == 'err','result':'OK','session_id':'sid1'}), flush=True)\n")
    def run_(self, mode, **kw):
        return run_claude([sys.executable, str(self.d / "fake.py"), mode], self.d, None, self.d / "raw", self.d / "err", **kw)
    def test_success_and_progress(self):
        state.jobs.clear(); state.new_job("k", url="u", sha="abcdef1234", repo="r")
        self.assertEqual(self.run_("ok", key="k")["result"], "OK"); self.assertEqual(state.jobs["k"]["activity"], "planning the review")
    def test_failures(self):
        for mode in ("fail", "err"):
            with self.assertRaises(RuntimeError): self.run_(mode)
    def test_timeout(self):
        t = time.time()
        with self.assertRaises(RuntimeError) as cm: self.run_("slow", timeout=1)
        self.assertIn("timed out", str(cm.exception)); self.assertLess(time.time() - t, 10)
    def test_stop(self):
        out = {}
        def go():
            try: self.run_("slow", stop_key=("C", "T"), timeout=60)
            except Exception as e: out["e"] = e
        th = threading.Thread(target=go); th.start()
        for _ in range(100):
            if ("C", "T") in state.procs: break
            time.sleep(0.05)
        self.assertEqual(state.stop_thread("C", "T"), "running"); th.join(10)
        self.assertIsInstance(out.get("e"), Stopped); self.assertNotIn(("C", "T"), state.procs); self.assertNotIn(("C", "T"), state.stopped)

class QA(unittest.TestCase):
    def test_session_store(self):
        f = Path(tempfile.mkdtemp()) / "s.json"; s = qa.SessionStore(f); self.assertIsNone(s.get("C", "1"))
        s.put("C", "1", session_id="abc", repo="r", turns=1); s.put("C", "1", turns=2)
        self.assertEqual(s.get("C", "1")["turns"], 2); self.assertEqual(qa.SessionStore(f).get("C", "1")["session_id"], "abc")
        self.assertEqual(s.expire(now=time.time() + 60), 0); self.assertEqual(s.expire(now=time.time() + qa.IDLE_HOURS * 3600 + 5), 1)
        f.write_text("{bad"); self.assertIsNone(qa.SessionStore(f).get("C", "1"))
    def test_build_command(self):
        (Path(_env.DATA) / "people.json").write_text(json.dumps({"UX": {"name": "Ada Lovelace", "github": "ada-l"}}))
        c = qa.build_command("q", None)
        self.assertNotIn("--resume", c); self.assertIn("--bare", c, "bare is the default"); self.assertEqual(c[c.index("--tools") + 1], "Bash", "the only tool is Bash")
        allowed = c[c.index("--allowedTools") + 1]; self.assertTrue(allowed.startswith(f"Bash({tools.BOTQ}:*)")); self.assertNotIn("Read", allowed); self.assertNotIn("git -C", allowed)
        flat = " ".join(c[c.index("--append-system-prompt") + 1].split())
        for needle in ("Ada Lovelace", "ada-l", tools.BOTQ, "[From:", "ONE simple command per call", "origin/main", "Ask questions when they help", "fe-workspace (degreed/fe-workspace", "degreed-api-version-2"): self.assertIn(needle, flat)
        self.assertNotIn(str(config.QA_DIR), flat, "no checkout paths exist any more"); self.assertEqual(qa.build_command("q", "sid9")[-2:], ["--resume", "sid9"])


class Fetch(unittest.TestCase):
    def test_fetch_moves_origin_main_only_never_the_working_tree(self):
        root = Path(tempfile.mkdtemp()); sh = lambda *a, cwd=root: subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
        sh("git", "init", "-q", "-b", "main", "o"); o = root / "o"
        for k, v in (("user.email", "t@t"), ("user.name", "t")): sh("git", "config", k, v, cwd=o)
        (o / "a.txt").write_text("v1"); sh("git", "add", "a.txt", cwd=o); sh("git", "commit", "-qm", "v1", cwd=o); sh("git", "clone", "-q", "o", "c"); c = root / "c"
        for k, v in (("user.email", "t@t"), ("user.name", "t")): sh("git", "config", k, v, cwd=c)
        sh("git", "checkout", "-q", "-b", "wip", cwd=c); (c / "a.txt").write_text("MY UNCOMMITTED WORK")
        before = (sh("git", "rev-parse", "HEAD", cwd=c), sh("git", "branch", "--show-current", cwd=c), (c / "a.txt").read_text())
        (o / "a.txt").write_text("v2"); sh("git", "commit", "-qam", "v2", cwd=o); new = sh("git", "rev-parse", "HEAD", cwd=o)
        qa._fetch_one("t/r", {"clone": str(c)})
        self.assertEqual(sh("git", "rev-parse", "origin/main", cwd=c), new, "origin/main moved")
        self.assertEqual((sh("git", "rev-parse", "HEAD", cwd=c), sh("git", "branch", "--show-current", cwd=c), (c / "a.txt").read_text()), before, "HEAD, branch and working tree untouched")
    def test_fetch_all_is_parallel_and_a_failure_is_logged_not_raised(self):
        seen = []
        def fake(key, cfg): seen.append(key); time.sleep(0.4)
        t = time.time()
        with mock.patch.object(qa, "_fetch_one", fake): qa.fetch_all()
        self.assertEqual(sorted(seen), sorted(config.ALL_REPOS)); self.assertLess(time.time() - t, 0.4 * len(config.ALL_REPOS) - 0.1, "not one after another")
        with mock.patch.object(qa.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "git", stderr=b"fatal: no access")): qa._fetch_one("x/y", {"clone": "/nope"})
    def test_the_prompt_lists_repos_by_name_and_states_the_scope_rules(self):
        flat = " ".join(qa.system_prompt().split())
        for needle in ("fe-workspace (degreed/fe-workspace, default branch main)", "whole `degreed`", "find <keywords...>", "ALWAYS say where you looked", "Never answer \"nobody\"", "unpushed work are never visible"): self.assertIn(needle, flat)
        self.assertNotIn("/qa/", flat)
    def test_readonly_repos_file_and_precedence(self):
        f = Path(_env.DATA) / "readonly_repos.json"
        f.write_text(json.dumps({"Degreed/Degreed": {"clone": "/x/mono"}, "degreed/fe-workspace": {"clone": "/x/other"}, "bad": "nope", "nocl": {}}))
        ro = config._readonly_repos(); self.assertEqual(list(ro), ["degreed/degreed", "degreed/fe-workspace"], "lowercased; entries without a clone dropped")
        merged = {**ro, **config.REPOS}; self.assertEqual(merged["degreed/fe-workspace"]["clone"], "/nonexistent/fe", "reviewable repos win a name clash")
        f.write_text("{bad"); self.assertEqual(config._readonly_repos(), {}); f.unlink(); self.assertEqual(config._readonly_repos(), {})


class Tools(unittest.TestCase):
    def test_review_tools(self):
        a, d = tools.review_tools(Path("/t/x"), post=True); self.assertIn("Bash(gh pr review:*)", a); self.assertIn("Edit(//t/x/**)", a)
        a2, _ = tools.review_tools(Path("/t/x"), post=False); self.assertNotIn("gh pr review", a2)
        for bad in ("gh pr merge", "git push", "git commit"): self.assertIn(bad, d)
    def test_qa_tools(self):
        a, d = tools.qa_tools()
        for ok in (f"Bash({tools.BOTQ}:*)", "Bash(gh pr list:*)", "Bash(gh search prs:*)"): self.assertIn(ok, a)
        for bad in ("Read", "Grep", "Glob", "git ", "Edit", "gh pr review", "gh pr merge", "Bash(cat"): self.assertNotIn(bad, a)
        for bad in ("Edit", "Write", "Read", "Grep", "Glob", "git push", "gh pr merge", "gh pr review"): self.assertIn(bad, d)

class People(unittest.TestCase):
    def test_lookup_reload_and_bad_file(self):
        from dex import people
        f = Path(_env.DATA) / "people.json"; f.write_text(json.dumps({"U1": {"name": "Ann", "github": "ann-g"}})); time.sleep(0.01)
        self.assertEqual(people.get("U1")["github"], "ann-g"); self.assertIsNone(people.get("U2")); self.assertIn("ann-g", people.known_text())
        f.write_text(json.dumps({"U2": {"name": "Bo"}})); os.utime(f, (time.time() + 5, time.time() + 5)); self.assertIsNone(people.get("U1")); self.assertEqual(people.get("U2")["name"], "Bo")
        f.write_text("{bad"); os.utime(f, (time.time() + 9, time.time() + 9)); self.assertEqual(people.all_people(), {})
        f.unlink(); self.assertEqual(people.known_text(), "(no people configured)")

class SlackUtils(unittest.TestCase):
    def test_format_and_helpers(self):
        self.assertEqual(su.to_slack("# T\n**b** [l](https://x.y)"), "*T*\n*b* <https://x.y|l>")
        self.assertEqual(su.to_slack("✅ repository ingested\n\nThe answer."), "The answer.")
        self.assertEqual(su.to_slack("mentions repository ingested pipeline inline"), "mentions repository ingested pipeline inline")
        self.assertTrue(su.to_slack("x" * 9000).endswith("…(truncated)") and len(su.to_slack("x" * 9000)) <= 3500)
        self.assertEqual(su.strip_mentions("<@U1ABC> who changed this? <@U2>"), "who changed this?")
    def test_render_mentions(self):
        (Path(_env.DATA) / "people.json").write_text(json.dumps({"U06": {"name": "Pat", "github": "pat-d"}, "U07": {"name": "Sam"}}))
        from dex import people; os.utime(Path(_env.DATA) / "people.json", (time.time() + 20, time.time() + 20)); su.bot_id = "UBOT"
        self.assertEqual(su.render_mentions("<@UBOT> what is <@U06> doing", owner="UOWNER"), "what is Pat (GitHub `pat-d`) doing")
        self.assertEqual(su.render_mentions("<@UOWNER> <@UBOT>: who is <@UNKNOWN1> and <@U07>?", owner="UOWNER"), "who is <@UNKNOWN1> and Sam?")
        self.assertEqual(su.render_mentions("<@U06|pat> hi"), "Pat (GitHub `pat-d`) hi"); su.bot_id = None
    def test_thread_info(self):
        class C:
            def conversations_replies(self, **k): return {"messages": [{"text": "PR https://x"}, {"text": "hi", "bot_id": "B1"}, {"text": "human"}, {"text": "last bot", "bot_id": "B1"}]}
        su.client = C(); i = su.thread_info("C", "1"); self.assertEqual((i["parent_text"], i["last_bot"]), ("PR https://x", "last bot"))
        class Bad:
            def conversations_replies(self, **k): raise RuntimeError("x")
        su.client = Bad(); self.assertEqual(su.thread_info("C", "1"), {"parent_text": "", "last_bot": None})

if __name__ == "__main__": unittest.main(verbosity=1)
