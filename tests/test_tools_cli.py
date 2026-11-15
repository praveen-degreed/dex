import _env  # noqa: F401  (must come first)
import io, json, unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock
from dex import tools_cli

def pr(n, title, updated, draft=False, **kw): return {"number": n, "title": title, "updatedAt": updated, "isDraft": draft, "url": f"https://x/pull/{n}", "reviewDecision": "APPROVED", "mergedAt": updated, "author": {"login": "bob"}, **kw}

def fake_run(calls):
    def run(args, **kw):
        calls.append(args)
        if "merged" in args: data = [pr(1, "merged one", "2026-11-12T10:00:00Z")]
        elif any(a.startswith("review-requested:") for a in args): data = [pr(9, "needs eyes", "2026-11-13T10:00:00Z")]
        else: data = [pr(2, "fresh", "2026-11-14T10:00:00Z", True), pr(3, "stale", "2026-05-01T10:00:00Z", True), pr(4, "stale too", "2026-06-01T10:00:00Z")]
        return SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
    return run

def cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err): rc = tools_cli.main(list(argv))
    return rc, out.getvalue(), err.getvalue()

class Botq(unittest.TestCase):
    def test_activity_groups_recent_and_summarises_stale(self):
        calls = []
        with mock.patch.object(tools_cli.subprocess, "run", fake_run(calls)), mock.patch.object(tools_cli, "since", lambda d: "2026-11-01"):
            rc, out, _ = cli("activity", "ann-g", "--days", "14")
        self.assertEqual(rc, 0); self.assertIn("== degreed/fe-workspace ==", out); self.assertIn("== degreed/degreed-coach-builder ==", out)
        self.assertIn("#2 [draft] fresh", out); self.assertNotIn("#3 ", out, "stale PRs are not listed one by one")
        self.assertIn("+ 2 older open PRs not updated since before 2026-11-01 (1 drafts; least recently updated 2026-05-01)", out)
        self.assertIn("Merged in the last 14 days (1):", out); self.assertIn("#9 needs eyes | by bob", out)
        self.assertTrue(all(isinstance(a, list) and a[:2] == ["gh", "pr"] for a in calls), "gh gets an argument list, never a shell string")
        self.assertTrue(any("merged:>=2026-11-01" in a for c in calls for a in c))
    def test_prs_open_lists_everything(self):
        with mock.patch.object(tools_cli.subprocess, "run", fake_run([])):
            rc, out, _ = cli("prs", "ann-g", "--state", "open")
        self.assertEqual(rc, 0); self.assertIn("#3 [draft] stale", out); self.assertIn("#4 stale too", out); self.assertNotIn("Merged", out)
    def test_a_capped_list_says_so_instead_of_reporting_a_false_total(self):
        many = lambda args, **kw: SimpleNamespace(returncode=0, stdout=json.dumps([pr(i, f"p{i}", "2026-11-14T10:00:00Z") for i in range(tools_cli.LIMIT)]), stderr="")
        with mock.patch.object(tools_cli.subprocess, "run", many):
            _, out, _ = cli("prs", "ann-g", "--state", "all")
        self.assertEqual(out.count(f"(list capped at {tools_cli.LIMIT}; there may be more)"), 4, "open + merged, in both repos")
        with mock.patch.object(tools_cli.subprocess, "run", fake_run([])):
            _, out, _ = cli("prs", "ann-g"); self.assertNotIn("capped", out)
    def test_rejects_bad_input_before_calling_gh(self):
        with mock.patch.object(tools_cli.subprocess, "run") as run:
            for bad in ("x; rm -rf /", "--repo", "a b", "$(id)", "", "-x", "a" * 40):
                try: rc, _, err = cli("activity", bad)
                except SystemExit: rc = 2
                self.assertEqual(rc, 2, bad)
            self.assertEqual(cli("activity", "ok", "--days", "0")[0], 2); self.assertEqual(cli("activity", "ok", "--days", "91")[0], 2)
            run.assert_not_called()
    def test_gh_failure_is_reported_not_raised(self):
        with mock.patch.object(tools_cli.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="HTTP 403")):
            rc, out, _ = cli("activity", "ann-g")
        self.assertEqual(rc, 0); self.assertIn("could not read (HTTP 403)", out)

def search_hit(repo, n, title, author, updated, state="OPEN", draft=False): return {"repository": {"nameWithOwner": repo}, "number": n, "title": title, "author": {"login": author}, "updatedAt": updated, "state": state, "isDraft": draft, "url": f"https://github.com/{repo}/pull/{n}"}

class Find(unittest.TestCase):
    def run_find(self, hits, *argv):
        calls = []
        def run(args, **kw): calls.append(args); return SimpleNamespace(returncode=0, stdout=json.dumps(hits), stderr="")
        with mock.patch.object(tools_cli.subprocess, "run", run), mock.patch.object(tools_cli, "since", lambda d: "2026-10-01"):
            rc, out, err = cli("find", *argv)
        return rc, out, err, calls
    def test_groups_by_repo_and_author_and_states_its_scope(self):
        hits = [search_hit("degreed/Degreed", 57490, "PD-129390 - Maestro ReBAC", "sudha", "2026-11-15T10:00:00Z"), search_hit("degreed/fe-workspace", 4460, "ReBAC gating", "praveen", "2026-11-14T10:00:00Z", draft=True), search_hit("degreed/Degreed", 5, "other", "sudha", "2026-11-01T10:00:00Z")]
        rc, out, _, calls = self.run_find(hits, "rebac", "maestro")
        self.assertEqual(rc, 0); self.assertIn("Searched open PRs across the whole `degreed` GitHub org for: rebac maestro", out)
        self.assertIn("3 result(s). By repo: degreed/Degreed 2, degreed/fe-workspace 1", out); self.assertIn("By author: sudha 2, praveen 1", out)
        self.assertIn("degreed/fe-workspace#4460 [draft] ReBAC gating | praveen | open", out)
        c = calls[0]; self.assertEqual(c[:4], ["gh", "search", "prs", "--owner"]); self.assertEqual(c[4], "degreed"); self.assertEqual(c[-2:], ["--", "rebac maestro"], "the query follows `--`, so a term can never be read as a flag")
        self.assertNotIn("--updated", c)
    def test_state_and_days_flags(self):
        _, _, _, calls = self.run_find([], "x", "--state", "merged", "--days", "30"); self.assertIn("--merged", calls[0]); self.assertEqual(calls[0][calls[0].index("--updated") + 1], ">=2026-10-01")
        _, _, _, calls = self.run_find([], "x", "--state", "all"); self.assertNotIn("--merged", calls[0]); self.assertNotIn("--state", calls[0])
    def test_no_results_still_says_where_it_looked(self):
        _, out, _, _ = self.run_find([], "nonesuch"); self.assertIn("Searched open PRs across the whole `degreed` GitHub org for: nonesuch", out); self.assertIn("No results", out)
    def test_cap_is_reported(self):
        _, out, _, _ = self.run_find([search_hit("a/b", i, f"t{i}", "u", "2026-11-14T10:00:00Z") for i in range(tools_cli.LIMIT)], "x")
        self.assertIn(f"{tools_cli.LIMIT}+ result(s)", out); self.assertIn("capped", out); self.assertLessEqual(out.count("a/b#"), 25, "only the newest 25 are listed")
    def test_bad_terms_never_reach_gh(self):
        with mock.patch.object(tools_cli.subprocess, "run") as run:
            for bad in (["-x"], ["--owner", "evil"], ["a;b"], ["$(id)"], ["a" * 61], ["t"] * 9, ["`x`"]):
                try: rc, _, _ = cli("find", *bad)
                except SystemExit: rc = 2
                self.assertEqual(rc, 2, bad)
            self.assertEqual(cli("find", "x", "--days", "400")[0], 2); run.assert_not_called()
    def test_gh_failure_is_an_error_not_a_traceback(self):
        with mock.patch.object(tools_cli.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="rate limited")):
            rc, _, err = cli("find", "x")
        self.assertEqual(rc, 1); self.assertIn("rate limited", err)

if __name__ == "__main__": unittest.main(verbosity=1)
