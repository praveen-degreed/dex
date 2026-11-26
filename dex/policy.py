"""Who may do what, how much, and a record of it.

Everything is configurable in `policy.json` and everything defaults to *allowed and unlimited*: with no
policy file the bot behaves as if every person may use every capability without a limit. Usage is always
recorded (so you can see it) but only enforced where a limit is set. A limit of null/absent = unlimited.

policy.json (all keys optional):
{
  "default_role": "member",
  "users": {"U0EXAMPLE01": "owner", "U0EXAMPLE02": "investigator"},
  "roles": {
    "member": {"can": ["review", "question", "status", "stop", "usage", "help"],
               "daily": {"question": 10, "review": 3}, "daily_usd": 2.0},
    "owner":  {"can": ["*"]}
  },
  "global": {"daily_usd": 15.0, "daily_router_calls": 500},
  "paused": false,                         # true = no costly work for anyone (status/stop/usage/help still answer)
  "disabled": ["review"],                  # capabilities switched off for everyone
  "max_job_usd": {"review": 4.0, "question": 0.5},   # per-job spending cap (soft: checked between model turns)
  "max_pr_files": 80                       # reviews refuse PRs with more changed files (the owner can force)
}
Capabilities: review, question, investigate, data, status, stop, usage, help.
"""
import datetime
import json
import threading
from pathlib import Path

ALL = ["review", "question", "investigate", "data", "status", "stop", "usage", "help"]
COUNTED = {"review", "question", "investigate", "data"}  # capabilities that spend tokens and count toward limits
DEFAULT = {"default_role": "member", "users": {}, "roles": {}, "global": {}}


class Policy:
    def __init__(self, data_dir, owner, path=None):
        d = Path(data_dir)
        self.owner = owner
        self.path = Path(path) if path else d / "policy.json"
        self.usage_path = d / "usage.json"
        self.audit_path = d / "audit.jsonl"
        self.lock = threading.Lock()
        self._mtime = None
        self.cfg = self._load_cfg()
        self.usage = self._load_usage()

    # --- configuration ------------------------------------------------------------------------
    def _load_cfg(self):
        try:
            self._mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime = None
        try:
            cfg = json.loads(self.path.read_text())
            return {**DEFAULT, **cfg} if isinstance(cfg, dict) else dict(DEFAULT)
        except (OSError, ValueError):
            return dict(DEFAULT)

    def reload(self):
        with self.lock:
            self.cfg = self._load_cfg()

    def maybe_reload(self):
        """Pick up edits to policy.json without a restart (cheap: one stat call)."""
        try:
            m = self.path.stat().st_mtime
        except OSError:
            m = None
        if m != self._mtime:
            self.reload()

    def role_of(self, user):
        if user == self.owner:
            return "owner"
        return self.cfg.get("users", {}).get(user) or self.cfg.get("default_role", "member")

    def paused(self):
        return bool(self.cfg.get("paused"))

    def disabled(self):
        return sorted(set(self.cfg.get("disabled") or []))

    def max_job_usd(self, capability):
        """The per-job spending cap for a capability, or None for no cap."""
        v = self.cfg.get("max_job_usd")
        v = v.get(capability) if isinstance(v, dict) else v
        try:
            return float(v) if v else None
        except (TypeError, ValueError):
            return None

    def claude_profile(self, kind):
        """Which Claude profile (see claude_env) a kind of run uses: policy.json `claude_profile`, a name for all kinds
        or {"review": "subscription", "all": "foundry"}; default foundry (the environment Dex was started with)."""
        v = self.cfg.get("claude_profile")
        if isinstance(v, dict):
            v = v.get(kind) or v.get("all")
        return v if isinstance(v, str) and v else "foundry"

    def big_pr(self):
        """(files, lines): a PR above either is 'big': Dex asks whether it can be split before spending on it. Defaults 50 files /
        3000 changed lines; policy.json `big_pr` {"files": n, "lines": n} changes them, 0 turns one off."""
        v = self.cfg.get("big_pr")
        v = v if isinstance(v, dict) else {}

        def lim(key, default):
            x = v.get(key, default)
            try:
                return int(x) if x else None
            except (TypeError, ValueError):
                return default
        return lim("files", 50), lim("lines", 3000)

    def max_pr_files(self):
        try:
            return int(self.cfg.get("max_pr_files")) or None
        except (TypeError, ValueError):
            return None

    def role_cfg(self, role):
        return self.cfg.get("roles", {}).get(role, {})

    def can(self, role, capability):
        cap = self.role_cfg(role).get("can")
        if role == "owner" and cap is None:
            return True
        return True if cap is None else ("*" in cap or capability in cap)  # no list = everything allowed

    def limit(self, role, capability):
        return (self.role_cfg(role).get("daily") or {}).get(capability)  # None = unlimited

    # --- usage --------------------------------------------------------------------------------
    @staticmethod
    def _today():
        return datetime.date.today().isoformat()

    def _load_usage(self):
        try:
            u = json.loads(self.usage_path.read_text())
            if u.get("date") == self._today():
                return u
        except (OSError, ValueError):
            pass
        return {"date": self._today(), "users": {}, "global_usd": 0.0, "router_calls": 0}

    def _roll(self):
        if self.usage.get("date") != self._today():
            self.usage = {"date": self._today(), "users": {}, "global_usd": 0.0, "router_calls": 0}

    def _save(self):
        tmp = self.usage_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.usage))
        tmp.replace(self.usage_path)

    def _user(self, user):
        rec = self.usage["users"].setdefault(user, {"usd": 0.0, "counts": {}})
        rec.setdefault("tokens", {"fresh": 0, "cache_read": 0, "output": 0})
        return rec

    @staticmethod
    def resets_in():
        now = datetime.datetime.now()
        midnight = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        mins = int((midnight - now).total_seconds() // 60)
        return f"{mins // 60}h {mins % 60:02d}m"

    # --- decisions ----------------------------------------------------------------------------
    def _check(self, user, capability):
        role = self.role_of(user)
        if capability in COUNTED:
            if self.cfg.get("paused"):
                return False, "Dex is paused by the owner right now, so it isn't taking new work. Status, stop and usage still work."
            if capability in set(self.cfg.get("disabled") or []):
                return False, f"`{capability}` is switched off by the owner right now."
        if not self.can(role, capability):
            return False, f"Your role (`{role}`) doesn't include `{capability}`. Ask the owner if you need it."
        if capability in COUNTED:
            lim = self.limit(role, capability)
            used = self._user(user)["counts"].get(capability, 0)
            if lim is not None and used >= lim:
                return False, f"You've used your {lim} `{capability}` request(s) for today. It resets in {self.resets_in()}."
            usd_lim = self.role_cfg(role).get("daily_usd")
            if usd_lim is not None and self._user(user)["usd"] >= usd_lim:
                return False, f"You've reached today's usage budget. It resets in {self.resets_in()}."
            g = (self.cfg.get("global") or {}).get("daily_usd")
            if g is not None and self.usage["global_usd"] >= g:
                return False, f"The bot has reached its daily budget. It resets in {self.resets_in()}."
        return True, ""

    def check(self, user, capability):
        """Permission and limits, without counting a request. Returns (ok, message)."""
        with self.lock:
            self._roll()
            return self._check(user, capability)

    def acquire(self, user, capability):
        """Atomically check and count a request that is about to start real work. Returns (ok, message)."""
        with self.lock:
            self._roll()
            ok, why = self._check(user, capability)
            if ok and capability in COUNTED:
                c = self._user(user)["counts"]
                c[capability] = c.get(capability, 0) + 1
                self._save()
            return ok, why

    def add_cost(self, user, usd, tokens=None):
        """Add a finished job's dollars (and, if known, its tokens) to the person's and the global totals."""
        with self.lock:
            self._roll()
            rec = self._user(user)
            rec["usd"] += float(usd or 0)
            self.usage["global_usd"] += float(usd or 0)
            for k in ("fresh", "cache_read", "output"):
                n = int((tokens or {}).get(k) or 0)
                rec["tokens"][k] += n
                g = self.usage.setdefault("tokens", {"fresh": 0, "cache_read": 0, "output": 0})
                g[k] = g.get(k, 0) + n
            self._save()

    def router_call_allowed(self):
        """Count a router call; False if the configured daily cap is reached (None = unlimited)."""
        with self.lock:
            self._roll()
            cap = (self.cfg.get("global") or {}).get("daily_router_calls")
            if cap is not None and self.usage["router_calls"] >= cap:
                return False
            self.usage["router_calls"] += 1
            return True

    def snapshot(self):
        """A plain-data copy of today's usage and the switches, for the dashboard."""
        with self.lock:
            self._roll()
            users = {u: {**json.loads(json.dumps(r)), "role": self.role_of(u)} for u, r in self.usage["users"].items()}
            return {"date": self.usage["date"], "users": users, "global_usd": self.usage["global_usd"],
                    "tokens": dict(self.usage.get("tokens") or {}), "router_calls": self.usage["router_calls"],
                    "paused": bool(self.cfg.get("paused")), "disabled": sorted(set(self.cfg.get("disabled") or [])),
                    "max_job_usd": self.cfg.get("max_job_usd"), "max_pr_files": self.cfg.get("max_pr_files"),
                    "claude_profile": self.cfg.get("claude_profile"),
                    "global": dict(self.cfg.get("global") or {})}

    def summary(self, user, everyone=False):
        """A short usage report for one person, or for everyone (owner)."""
        with self.lock:
            self._roll()

            def line(u, rec):
                role = self.role_of(u)
                parts = []
                for cap in ("review", "question", "investigate", "data"):
                    used = rec["counts"].get(cap, 0)
                    lim = self.limit(role, cap)
                    if used or lim is not None:
                        parts.append(f"{cap} {used}" + (f"/{lim}" if lim is not None else ""))
                tk = rec.get("tokens") or {}
                spent = f", {human(sum(tk.values()))} tokens (fresh {human(tk.get('fresh', 0))}, cached {human(tk.get('cache_read', 0))}, out {human(tk.get('output', 0))})" if sum(tk.values()) else ""
                return f"<@{u}> ({role}): " + (", ".join(parts) or "no requests yet") + f", ${rec['usd']:.2f}" + spent

            if everyone:
                rows = [line(u, r) for u, r in sorted(self.usage["users"].items(), key=lambda kv: -kv[1]["usd"])]
                head = f"Today's usage (resets in {self.resets_in()}), total ${self.usage['global_usd']:.2f}, router calls {self.usage['router_calls']}"
                return head + ("\n" + "\n".join(rows) if rows else "\nNo requests yet.")
            rec = self.usage["users"].get(user, {"usd": 0.0, "counts": {}})
            return line(user, rec) + f". Resets in {self.resets_in()}."

    # --- audit --------------------------------------------------------------------------------
    def audit(self, **fields):
        """Append one JSON line describing a decision or action. Never raises."""
        try:
            rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), **fields}
            with self.audit_path.open("a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:
            pass


def human(n):
    """1234 -> 1.2K, 1500000 -> 1.5M"""
    n = float(n)
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.1f}K" if n >= 1e3 else f"{n:.0f}"


def edit_file(path, mutate):
    """Change policy.json safely from any process: read it, apply `mutate(dict)`, write it atomically.

    Unknown keys are kept. A missing or unreadable file starts from an empty policy. Dex picks the change up on the
    next message without a restart. Returns the new content.
    """
    path = Path(path)
    try:
        cfg = json.loads(path.read_text())
        cfg = cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        cfg = {}
    mutate(cfg)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.replace(path)
    return cfg
