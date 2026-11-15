import _env  # noqa: F401  (must come first)
import io, os, subprocess, tempfile, unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
from dex import config, tools_cli

ROOT = Path(tempfile.mkdtemp(prefix="botq-"))
def sh(*a, cwd=ROOT, env=None): return subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True, env={**os.environ, **(env or {})}).stdout.strip()
def commit(repo, msg, author="Ann <ann@x>", date="2026-01-10T10:00:00"):
    sh("git", "add", "-A", cwd=repo); sh("git", "commit", "-qm", msg, "--author", author, cwd=repo, env={"GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date})
    return sh("git", "rev-parse", "HEAD", cwd=repo)

def build():
    """origin (main) -> clone. The clone then gets everything that must NEVER be visible to the assistant."""
    sh("git", "init", "-q", "-b", "main", "origin"); o = ROOT / "origin"
    for k, v in (("user.email", "t@t"), ("user.name", "t")): sh("git", "config", k, v, cwd=o)
    (o / "src").mkdir(); (o / "src/app.py").write_text("def alpha():\n    return 'main-v1'\n\ndef beta():\n    return 1\n")
    (o / "README.md").write_text("hello\n"); (o / "bin.dat").write_bytes(b"\x00\x01\x02binary")
    c1 = commit(o, "feat: alpha", "Ann <ann@x>", "2026-01-10T10:00:00")
    (o / "src/app.py").write_text("def alpha():\n    return 'main-v2'\n\ndef beta():\n    return 1\n"); c2 = commit(o, "fix: change alpha (#12)", "Bob <bob@x>", "2026-02-10T10:00:00")
    (o / "big.txt").write_text("x" * (tools_cli.MAX_BLOB + 5)); (o / "many.txt").write_text("needle\n" * 300); commit(o, "chore: big files", "Ann <ann@x>", "2026-03-01T10:00:00")
    sh("git", "clone", "-q", "origin", "clone"); c = ROOT / "clone"
    for k, v in (("user.email", "t@t"), ("user.name", "t")): sh("git", "config", k, v, cwd=c)
    sh("git", "checkout", "-q", "-b", "my-wip", cwd=c)
    (c / "src/app.py").write_text("def alpha():\n    return 'LOCAL-UNPUSHED'\n"); (c / "secret_local.py").write_text("SECRET = 'local-only'\n"); local = commit(c, "wip: local only", "Me <me@x>", "2026-04-01T10:00:00")
    (c / "src/app.py").write_text("DIRTY WORKING TREE\n"); (c / ".env").write_text("TOKEN=hunter2\n")
    return {"c1": c1, "c2": c2, "local": local}

SHAS = build()
REPOS = {"acme/widgets": {"clone": str(ROOT / "clone"), "default_branch": "main"}}

def cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(config.ALL_REPOS, REPOS, clear=True), redirect_stdout(out), redirect_stderr(err):
        try: rc = tools_cli.main(list(argv))
        except SystemExit as e: rc = e.code
    return rc, out.getvalue(), err.getvalue()

class ReadsMainOnly(unittest.TestCase):
    def test_show_is_main_never_the_working_tree_or_a_local_branch(self):
        rc, out, _ = cli("show", "widgets", "src/app.py"); self.assertEqual(rc, 0)
        self.assertIn("main-v2", out); self.assertNotIn("DIRTY", out); self.assertNotIn("LOCAL-UNPUSHED", out); self.assertIn("@ origin/main", out); self.assertIn("1  def alpha", out.replace("    ", " "))
    def test_untracked_and_local_only_files_do_not_exist_for_the_assistant(self):
        for path in (".env", "secret_local.py"):
            rc, _, err = cli("show", "widgets", path); self.assertEqual(rc, 1, path); self.assertIn("not found on origin/main", err)
        _, out, _ = cli("ls", "widgets"); self.assertNotIn(".env", out); self.assertNotIn("secret_local", out); self.assertIn("README.md", out)
        _, out, _ = cli("grep", "widgets", "hunter2"); self.assertIn("no matches", out); _, out, _ = cli("grep", "widgets", "local-only"); self.assertIn("no matches", out)
    def test_log_and_blame_and_commit_ignore_local_commits(self):
        _, out, _ = cli("log", "widgets"); self.assertNotIn("wip: local only", out); self.assertIn("fix: change alpha (#12)", out)
        _, out, _ = cli("blame", "widgets", "src/app.py", "--from", "2", "--to", "2"); self.assertIn("Bob", out); self.assertIn("main-v2", out); self.assertNotIn("LOCAL", out)
        rc, _, err = cli("commit", "widgets", SHAS["local"]); self.assertEqual(rc, 1); self.assertIn("is not a commit on origin/main", err)
        rc, out, _ = cli("commit", "widgets", SHAS["c2"][:10]); self.assertEqual(rc, 0); self.assertIn("fix: change alpha", out); self.assertIn("src/app.py", out)
        _, out, _ = cli("commit", "widgets", SHAS["c2"], "--path", "src/app.py"); self.assertIn("-    return 'main-v1'", out); self.assertIn("+    return 'main-v2'", out)

class Features(unittest.TestCase):
    def test_show_slice_limits_and_refusals(self):
        _, out, _ = cli("show", "widgets", "src/app.py", "--from", "4", "--to", "5"); self.assertIn("lines 4-5 of 5", out); self.assertIn("def beta", out); self.assertNotIn("alpha", out)
        rc, _, err = cli("show", "widgets", "big.txt"); self.assertEqual(rc, 1); self.assertIn("too large", err)
        rc, _, err = cli("show", "widgets", "bin.dat"); self.assertEqual(rc, 1); self.assertIn("binary", err)
    def test_ls_directory_and_recursive(self):
        _, out, _ = cli("ls", "widgets", "src"); self.assertIn("src/app.py", out); _, out, _ = cli("ls", "widgets", "-r"); self.assertIn("src/app.py", out); self.assertIn("README.md", out)
    def test_grep_modes_and_cap(self):
        _, out, _ = cli("grep", "widgets", "def alpha"); self.assertIn("src/app.py:1:def alpha", out); self.assertNotIn("origin/main:src", out)
        _, out, _ = cli("grep", "widgets", "DEF ALPHA"); self.assertIn("no matches", out); _, out, _ = cli("grep", "widgets", "DEF ALPHA", "-i"); self.assertIn("src/app.py", out)
        _, out, _ = cli("grep", "widgets", "def (alpha|beta)"); self.assertIn("no matches", out, "fixed-string by default: regex characters are literal"); _, out, _ = cli("grep", "widgets", "def (alpha|beta)", "--regex"); self.assertIn("def beta", out)
        _, out, _ = cli("grep", "widgets", "needle", "--path", "many.txt"); self.assertIn("...(220 more matches not shown", out); self.assertEqual(out.count("many.txt:"), 80)
        _, out, _ = cli("grep", "widgets", "def alpha", "--path", "*.md"); self.assertIn("no matches", out)
    def test_log_filters(self):
        _, out, _ = cli("log", "widgets", "--author", "Ann"); self.assertIn("feat: alpha", out); self.assertNotIn("fix: change", out)
        _, out, _ = cli("log", "widgets", "--search", "main-v2"); self.assertIn("fix: change alpha", out); self.assertNotIn("feat: alpha", out)
        _, out, _ = cli("log", "widgets", "--grep", "CHORE"); self.assertIn("big files", out); _, out, _ = cli("log", "widgets", "--since", "2026-02-15"); self.assertIn("big files", out); self.assertNotIn("feat: alpha", out)
        _, out, _ = cli("log", "widgets", "--path", "src/app.py"); self.assertEqual(out.count("\n"), 3, "header + 2 commits touching the file"); _, out, _ = cli("log", "widgets", "--max", "1"); self.assertEqual(len(out.strip().splitlines()), 2)

class Rejects(unittest.TestCase):
    def test_bad_input_is_refused_before_any_effect(self):
        marker = ROOT / "pwned"
        for argv in (["show", "widgets", "../etc/passwd"], ["show", "widgets", "/etc/passwd"], ["show", "widgets", "-x"], ["show", "widgets", ":(top)README.md"], ["show", "widgets", "src/*.py"],
                     ["ls", "widgets", "../"], ["grep", "widgets", ""], ["grep", "widgets", "a" * 201], ["grep", "widgets", "a\nb"], ["grep", "widgets", "x", "--path", "../x"],
                     ["log", "widgets", "--since", "yesterday"], ["log", "widgets", "--author", "a;b"], ["log", "widgets", "--path", "-p"], ["blame", "widgets", "src/app.py", "--from", "0", "--to", "3"],
                     ["blame", "widgets", "src/app.py", "--from", "1", "--to", "500"], ["commit", "widgets", "HEAD"], ["commit", "widgets", "xyz"], ["commit", "widgets", "main"], ["show", "nope", "x"]):
            rc, _, err = cli(*argv); self.assertEqual(rc, 2, argv); self.assertIn("error", err, argv)  # ours, or the argument parser's own
        self.assertFalse(marker.exists())
    def test_no_shell_so_metacharacters_are_just_text(self):
        marker = ROOT / "pwned"; rc, out, _ = cli("grep", "widgets", f"x; touch {marker}"); self.assertEqual(rc, 0); self.assertIn("no matches", out); self.assertFalse(marker.exists())
        rc, out, _ = cli("log", "widgets", "--search", f"$(touch {marker})"); self.assertEqual(rc, 0); self.assertFalse(marker.exists())
    def test_unknown_repo_lists_the_known_ones(self):
        rc, _, err = cli("show", "nope", "x"); self.assertEqual(rc, 2); self.assertIn("known repos: widgets", err)

if __name__ == "__main__": unittest.main(verbosity=1)
