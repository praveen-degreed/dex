import _env  # noqa: F401  (must come first)
import io, json, os, tempfile, unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
from dex import claude_env, config, ctl
from dex.claude_env import ClaudeProfileError

DATA = Path(_env.DATA)
BASE = {"PATH": "/bin", "HOME": "/h", "CLAUDE_CODE_USE_FOUNDRY": "1", "ANTHROPIC_FOUNDRY_RESOURCE": "res", "ANTHROPIC_FOUNDRY_API_KEY": "secret",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "dep-opus", "ANTHROPIC_DEFAULT_SONNET_MODEL": "dep-sonnet", "CLAUDE_CODE_SESSION_ID": "s1"}

class Pol:  # the one thing claude_env needs from the policy
    def __init__(self, v=None): self.v = v
    def claude_profile(self, kind):
        v = self.v.get(kind) or self.v.get("all") if isinstance(self.v, dict) else self.v
        return v if isinstance(v, str) and v else "foundry"

def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try: rc = ctl.main(list(argv))
        except SystemExit as e: rc = e.code
    return rc, out.getvalue(), err.getvalue()

class Env(unittest.TestCase):
    def setUp(self):
        self.cfgdir = tempfile.mkdtemp(); (DATA / "claude_profiles.json").write_text(json.dumps({"subscription": {"config_dir": self.cfgdir}}))
        self.addCleanup(lambda: (DATA / "claude_profiles.json").unlink(missing_ok=True))
    def test_foundry_is_the_environment_as_it_is_plus_extras(self):
        env = claude_env.env_for(Pol(), "review", {"TMPDIR": "/t/"}, base=BASE)
        self.assertEqual(env, {**BASE, "TMPDIR": "/t/"})
    def test_subscription_drops_the_foundry_variables_and_points_at_its_own_config_dir(self):
        env = claude_env.env_for(Pol("subscription"), "review", {"TMPDIR": "/t/"}, base=BASE)
        for gone in ("CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_FOUNDRY_RESOURCE", "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL"):
            self.assertNotIn(gone, env, gone)
        self.assertEqual((env["CLAUDE_CONFIG_DIR"], env["TMPDIR"], env["PATH"], env["CLAUDE_CODE_SESSION_ID"]), (self.cfgdir, "/t/", "/bin", "s1"))
        self.assertEqual(BASE["CLAUDE_CODE_USE_FOUNDRY"], "1", "the caller's environment is not modified")
    def test_a_per_kind_profile(self):
        pol = Pol({"review": "subscription"})
        self.assertIn("CLAUDE_CODE_USE_FOUNDRY", claude_env.env_for(pol, "question", base=BASE)); self.assertNotIn("CLAUDE_CODE_USE_FOUNDRY", claude_env.env_for(pol, "review", base=BASE))
    def test_it_fails_closed(self):
        with self.assertRaises(ClaudeProfileError): claude_env.env_for(Pol("nope"), "review", base=BASE)
        (DATA / "claude_profiles.json").write_text(json.dumps({"subscription": {"config_dir": "/no/such/dir"}}))
        with self.assertRaises(ClaudeProfileError) as c: claude_env.env_for(Pol("subscription"), "review", base=BASE)
        self.assertIn("/login", str(c.exception))
    def test_a_key_comes_from_a_named_environment_variable_never_from_the_file(self):
        (DATA / "claude_profiles.json").write_text(json.dumps({"mine": {"unset": ["ANTHROPIC_FOUNDRY_*", "CLAUDE_CODE_USE_FOUNDRY"], "set_from_env": {"ANTHROPIC_API_KEY": "MY_KEY_VAR"}}}))
        with self.assertRaises(ClaudeProfileError) as c: claude_env.env_for(Pol("mine"), "review", base=BASE)
        self.assertIn("MY_KEY_VAR", str(c.exception))
        env = claude_env.env_for(Pol("mine"), "review", base={**BASE, "MY_KEY_VAR": "sk-x"}); self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-x"); self.assertNotIn("ANTHROPIC_FOUNDRY_RESOURCE", env)
    def test_bare_is_dropped_only_off_the_default_profile(self):
        cmd = ["claude", "-p", "x", "--bare", "--model", "haiku"]
        self.assertEqual(claude_env.adapt_command(Pol(), "router", cmd), cmd)
        self.assertEqual(claude_env.adapt_command(Pol("subscription"), "router", cmd), ["claude", "-p", "x", "--model", "haiku"])

class Switch(unittest.TestCase):
    def setUp(self):
        for f in ("policy.json",): (DATA / f).unlink(missing_ok=True)
        self.cfgdir = tempfile.mkdtemp(); (DATA / "claude_profiles.json").write_text(json.dumps({"subscription": {"config_dir": self.cfgdir}}))
        self.addCleanup(lambda: [(DATA / f).unlink(missing_ok=True) for f in ("claude_profiles.json", "policy.json")])
    def pol(self): return json.loads((DATA / "policy.json").read_text())
    def test_use_show_and_back_to_the_default(self):
        rc, out, _ = run("claude", "use", "review", "subscription"); self.assertEqual(rc, 0); self.assertEqual(self.pol()["claude_profile"], {"review": "subscription"})
        self.assertIn("review=subscription", out); self.assertIn("question=foundry", out); self.assertIn("Reminder", out)
        run("claude", "use", "all", "subscription"); self.assertEqual(set(self.pol()["claude_profile"]), {"review", "question", "router"})
        rc, out, _ = run("claude", "use", "all", "foundry"); self.assertNotIn("claude_profile", self.pol()); self.assertIn("review=foundry", out); self.assertNotIn("Reminder", out)
        rc, out, _ = run("claude", "show"); self.assertIn("Known profiles: foundry, subscription", out)
    def test_one_kind_changes_alone_and_an_unknown_name_is_refused(self):
        run("claude", "use", "all", "subscription"); run("claude", "use", "router", "foundry"); self.assertEqual(self.pol()["claude_profile"], {"review": "subscription", "question": "subscription"})
        rc, _, err = run("claude", "use", "review", "typo"); self.assertEqual(rc, 2); self.assertIn("unknown profile", err); rc, _, err = run("claude", "use", "bogus", "foundry"); self.assertIn("unknown kind", err)
    def test_an_old_blanket_string_setting_is_expanded_not_lost(self):
        (DATA / "policy.json").write_text(json.dumps({"claude_profile": "subscription"})); run("claude", "use", "question", "foundry")
        self.assertEqual(self.pol()["claude_profile"], {"review": "subscription", "router": "subscription"})
    def test_the_two_shortcuts(self):
        rc, out, _ = run("subscription"); self.assertEqual(rc, 0); self.assertEqual(self.pol()["claude_profile"], {"review": "subscription"}); self.assertIn("review=subscription, question=foundry", out)
        run("claude", "use", "question", "subscription"); rc, out, _ = run("foundry"); self.assertEqual(rc, 0); self.assertNotIn("claude_profile", self.pol()); self.assertIn("review=foundry, question=foundry, router=foundry", out)
    def test_check_reports_a_profile_that_cannot_be_used(self):
        (DATA / "claude_profiles.json").write_text(json.dumps({"subscription": {"config_dir": "/no/such/dir"}}))
        rc, out, _ = run("claude", "check", "subscription"); self.assertEqual(rc, 1); self.assertIn("not usable", out)

if __name__ == "__main__": unittest.main()
