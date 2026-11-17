"""Which Claude backend each kind of Dex run uses, and the environment that selects it.

Two ready-made profiles:
  foundry       the environment exactly as Dex was started (today: Azure Foundry). The default.
  subscription  a personal Claude subscription: a separate config dir (its own login and its own skills) and none of
                the Foundry / cloud-provider / API-key variables, so Claude Code falls back to its normal login.
claude_profiles.json (next to policy.json, ignored by git) can redefine either or add more; see claude_profiles.example.json:
  {"subscription": {"config_dir": "~/.claude-subscription", "unset": ["ANTHROPIC_FOUNDRY_*"], "set": {"K": "v"},
                    "set_from_env": {"ANTHROPIC_API_KEY": "MY_PERSONAL_KEY_VAR"}}}
Which profile a kind of run uses is a policy.json switch (`dexctl claude use review subscription`), picked up on the next run.
A run set to a profile that is missing or incomplete FAILS; it never falls back to the other account.
"""
import fnmatch
import json
import os
from pathlib import Path

from . import config

FOUNDRY, SUBSCRIPTION = "foundry", "subscription"
KINDS = ("review", "question", "router")  # what dexctl can switch ("all" sets every kind)

_BUILTIN = {
    FOUNDRY: {},
    SUBSCRIPTION: {
        "config_dir": "~/.claude-subscription",
        "unset": ["CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_FOUNDRY_*",
                  "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL",
                  "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_*"],
    },
}


class ClaudeProfileError(RuntimeError):
    pass


def profiles():
    """All profiles: the built-ins, overridden or extended by claude_profiles.json."""
    out = {k: dict(v) for k, v in _BUILTIN.items()}
    try:
        raw = json.loads((config.DATA / "claude_profiles.json").read_text())
    except (OSError, ValueError):
        raw = {}
    if isinstance(raw, dict):
        for name, prof in raw.items():
            if isinstance(prof, dict):
                out[name] = {**out.get(name, {}), **prof}
    return out


def profile_name(policy, kind):
    return policy.claude_profile(kind)


def env_for(policy, kind, extra=None, base=None):
    """The environment for one `claude` subprocess of this kind. Raises ClaudeProfileError if the profile cannot be used."""
    env = dict(os.environ if base is None else base)
    name = profile_name(policy, kind)
    prof = profiles().get(name)
    if prof is None:
        raise ClaudeProfileError(f"Claude profile '{name}' (set for {kind}) is not defined. Known: {', '.join(sorted(profiles()))}.")
    for pattern in prof.get("unset") or []:
        for key in [k for k in env if fnmatch.fnmatchcase(k, pattern)]:
            env.pop(key, None)
    for key, value in (prof.get("set") or {}).items():
        env[str(key)] = str(value)
    for key, source in (prof.get("set_from_env") or {}).items():
        value = (os.environ if base is None else base).get(source)
        if not value:
            raise ClaudeProfileError(f"Claude profile '{name}' needs the environment variable {source} (for {key}), which is not set.")
        env[str(key)] = value
    if prof.get("config_dir"):
        cfg = Path(os.path.expanduser(str(prof["config_dir"])))
        if not cfg.is_dir():
            raise ClaudeProfileError(f"Claude profile '{name}': its config dir {cfg} does not exist. Create it, log in with "
                                     f"`CLAUDE_CONFIG_DIR={cfg} claude` (then /login) and install the skills there (see the README).")
        env["CLAUDE_CONFIG_DIR"] = str(cfg)
    env.update(extra or {})
    return env


def adapt_command(policy, kind, cmd):
    """--bare skips the login, so a run on a login-based profile (not the default) must not use it."""
    if profile_name(policy, kind) == FOUNDRY:
        return cmd
    return [a for a in cmd if a != "--bare"]
