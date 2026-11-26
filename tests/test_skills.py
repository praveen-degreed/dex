import _env  # noqa: F401  (must come first)
import json, os, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock
from dex import config, skills

REPOS = {"acme/web": {"clone": "", "skill": "/review-prs"}, "acme/api": {"clone": "", "skill": "/review-prs-be"},
         "acme/mono": {"clone": "", "skill": "/review-prs-net {url}", "dex_posts": True}}

def fake_home(*names):
    h = Path(tempfile.mkdtemp(prefix="claude-")); [(h / "skills" / n).mkdir(parents=True) or (h / "skills" / n / "SKILL.md").write_text("x") for n in names]; return h

def fake_source(root):
    """A tiny skills repo: install.sh just creates <CLAUDE_HOME>/skills/<name>/SKILL.md for the asked kind."""
    src = Path(root) / "src"; src.mkdir()
    (src / "install.sh").write_text('#!/bin/bash\ncase "$1" in fe) n=review-prs;; be) n=review-prs-be;; net) n=review-prs-net;; esac\n'
                                    'mkdir -p "$CLAUDE_HOME/skills/$n" && echo ok > "$CLAUDE_HOME/skills/$n/SKILL.md"\n')
    return src

class Needed(unittest.TestCase):
    def test_skill_name_and_needed(self):
        self.assertEqual(skills.skill_name("/review-prs-net {url}"), "review-prs-net"); self.assertEqual(skills.skill_name(""), "")
        self.assertEqual(skills.needed(REPOS), {"review-prs": ["acme/web"], "review-prs-be": ["acme/api"], "review-prs-net": ["acme/mono"]})
        self.assertEqual(skills.needed({"a/b": {"skill": "/review-prs"}}), {"review-prs": ["a/b"]}, "only the skills the repos call")
        self.assertEqual(skills.needed({}), {})
    def test_missing_checks_every_home(self):
        a, b = fake_home("review-prs"), fake_home()
        got = skills.missing({"acme/web": REPOS["acme/web"]}, homes=[a, b]); self.assertEqual(got, [(b, "review-prs")])

class Install(unittest.TestCase):
    def test_installs_only_what_is_missing_into_every_home(self):
        home1, home2, src = fake_home("review-prs"), fake_home(), fake_source(tempfile.mkdtemp())
        repos = {"acme/web": REPOS["acme/web"], "acme/api": REPOS["acme/api"]}
        with mock.patch.object(skills, "claude_homes", return_value=[home1, home2]), mock.patch.object(skills, "fetch_source", return_value=src):
            self.assertEqual(skills.install(repos, _env.DATA, log=lambda *_: None), [])
        for h in (home1, home2):
            for n in ("review-prs", "review-prs-be"): self.assertTrue(skills.installed(h, n), (h, n))
    def test_nothing_missing_means_no_download(self):
        home = fake_home("review-prs")
        with mock.patch.object(skills, "claude_homes", return_value=[home]), mock.patch.object(skills, "fetch_source") as f:
            self.assertEqual(skills.install({"acme/web": REPOS["acme/web"]}, _env.DATA), []); f.assert_not_called()
    def test_update_reinstalls_even_when_present(self):
        home, src = fake_home("review-prs"), fake_source(tempfile.mkdtemp())
        with mock.patch.object(skills, "claude_homes", return_value=[home]), mock.patch.object(skills, "fetch_source", return_value=src) as f:
            skills.install({"acme/web": REPOS["acme/web"]}, _env.DATA, update=True, log=lambda *_: None); f.assert_called_once()
    def test_download_failure_is_reported_not_raised(self):
        home = fake_home()
        with mock.patch.object(skills, "claude_homes", return_value=[home]), mock.patch.object(skills, "fetch_source", side_effect=RuntimeError("no network")):
            self.assertEqual(skills.install({"acme/web": REPOS["acme/web"]}, _env.DATA), ["no network"])
    def test_unknown_skill_is_a_problem_not_a_crash(self):
        home, src = fake_home(), fake_source(tempfile.mkdtemp())
        with mock.patch.object(skills, "claude_homes", return_value=[home]), mock.patch.object(skills, "fetch_source", return_value=src):
            p = skills.install({"acme/x": {"skill": "/my-own-skill"}}, _env.DATA, log=lambda *_: None)
        self.assertEqual(len(p), 1); self.assertIn("unknown skill", p[0])

class Validate(unittest.TestCase):
    def test_repo_problems(self):
        clone = Path(tempfile.mkdtemp()); (clone / ".git").mkdir(); home = fake_home("review-prs")
        repos = {"ok/r": {"clone": str(clone), "skill": "/review-prs"}, "nogit/r": {"clone": "/nonexistent", "skill": "/review-prs"},
                 "noskill/r": {"clone": str(clone), "skill": "/review-prs-be"}, "empty/r": {"clone": str(clone), "skill": ""}}
        got = skills.repo_problems(repos, homes=[home])
        self.assertEqual(sorted(got), ["empty/r", "nogit/r", "noskill/r"]); self.assertIn("not a git repository", got["nogit/r"]); self.assertIn("not installed", got["noskill/r"])
    def test_ensure_disables_broken_repos_and_never_raises(self):
        import logging
        cfg = mock.Mock(REPOS={"a/b": {"clone": "/nonexistent", "skill": "/review-prs"}}, ALL_REPOS={"a/b": {}}, DATA=_env.DATA)
        with mock.patch.dict(os.environ, {"SKILLS_AUTO_INSTALL": "0"}), mock.patch.object(skills, "claude_homes", return_value=[fake_home()]):
            skills.ensure(cfg, logging.getLogger("t"))
        self.assertEqual(cfg.REPOS, {}); self.assertEqual(cfg.ALL_REPOS, {})
        with mock.patch.object(skills, "settings", side_effect=ValueError("boom")): skills.ensure(cfg, logging.getLogger("t"))
    def test_fetch_source_clones_a_real_local_repo(self):
        origin = Path(tempfile.mkdtemp()); subprocess.run(["git", "init", "-q", "-b", "main", str(origin)], check=True)
        (origin / "install.sh").write_text("#!/bin/bash\n"); subprocess.run(["git", "-C", str(origin), "add", "."], check=True)
        subprocess.run(["git", "-C", str(origin), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"], check=True)
        data = tempfile.mkdtemp()
        real = subprocess.run
        def rewrite(cmd, *a, **k):  # the URL is github; point it at the local repo instead
            return real([str(origin) if str(c).startswith("https://github.com/") else c for c in cmd], *a, **k)
        with mock.patch.object(skills.subprocess, "run", side_effect=rewrite):
            d = skills.fetch_source(data, "acme/review-skills", "main"); self.assertTrue((d / "install.sh").is_file())
            self.assertEqual(skills.fetch_source(data, "acme/review-skills", "main"), d, "second call refreshes the clone")

class Cli(unittest.TestCase):
    def test_no_repos_is_not_an_error(self):
        with mock.patch.object(config, "REPOS", {}): self.assertEqual(skills.main(["check"]), 0)
    def test_bad_command(self): self.assertEqual(skills.main(["nope"]), 2)

if __name__ == "__main__": unittest.main()
