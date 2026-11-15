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
  "global": {"daily_usd": 15.0, "daily_router_calls": 500}
}
Capabilities: review, question, investigate, data, status, stop, usage, help.
"""
import datetime
import json
import threading
import time
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
        return self.usage["users"].setdefault(user, {"usd": 0.0, "counts": {}})

    @staticmethod
    def resets_in():
        now = datetime.datetime.now()
        midnight = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        mins = int((midnight - now).total_seconds() // 60)
        return f"{mins // 60}h {mins % 60:02d}m"

    # --- decisions ----------------------------------------------------------------------------
    def _check(self, user, capability):
        role = self.role_of(user)
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

    def add_cost(self, user, usd):
        with self.lock:
            self._roll()
            self._user(user)["usd"] += float(usd or 0)
            self.usage["global_usd"] += float(usd or 0)
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

    def remaining(self, user, capability):
        with self.lock:
            self._roll()
            lim = self.limit(self.role_of(user), capability)
            return None if lim is None else max(0, lim - self._user(user)["counts"].get(capability, 0))

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
                return f"<@{u}> ({role}): " + (", ".join(parts) or "no requests yet") + f", ${rec['usd']:.2f}"

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
