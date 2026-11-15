"""dexctl: see what Dex is doing and change its switches.

    dexctl status                          what is running now, the switches and caps
    dexctl usage [--days N]                spend and tokens: today per person, and the last N days (default 7)
    dexctl top [--n N] [--days N]          the most expensive actions
    dexctl show                            the policy switches and caps
    dexctl pause | resume                  stop / allow all costly work (status, stop, usage, help keep working)
    dexctl disable <capability>... | enable <capability>...
    dexctl cap job-usd <review|question|all> <amount|off>    stop a single job that spends more (soft cap)
    dexctl cap daily-usd <amount|off>      the whole bot's spend per day
    dexctl cap user-usd <role> <amount|off>
    dexctl cap pr-files <n|off>            refuse reviews of PRs with more changed files
    dexctl serve [--port 8787]             a read-only dashboard on http://127.0.0.1:<port>

It reads the files Dex writes (runtime.json, usage.json, audit.jsonl, policy.json) and edits only policy.json, which
Dex picks up without a restart. It never talks to the running process.
"""
import argparse
import collections
import datetime
import json
import sys
import time

from . import config
from .policy import ALL, COUNTED, edit_file, human

POLICY = config.DATA / "policy.json"
STALE_S = 20  # runtime.json older than this means Dex is not running


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def read_audit(days=None):
    rows, cutoff = [], (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat() if days else ""
    try:
        for line in (config.DATA / "audit.jsonl").read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("ts", "") >= cutoff:
                rows.append(r)
    except OSError:
        pass
    return rows


def age(s):
    s = int(max(s, 0))
    return f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


def tok(t):
    t = t or {}
    return f"{human(sum(t.values()))} tokens (fresh {human(t.get('fresh', 0))}, cached {human(t.get('cache_read', 0))}, out {human(t.get('output', 0))})"


def caps_line(pol):
    parts = []
    mj = pol.get("max_job_usd")
    if mj:
        parts.append("per-job cap " + (", ".join(f"{k} ${v:g}" for k, v in mj.items() if v) if isinstance(mj, dict) else f"${mj:g}"))
    if pol.get("max_pr_files"):
        parts.append(f"PR files <= {pol['max_pr_files']}")
    if (pol.get("global") or {}).get("daily_usd"):
        parts.append(f"daily total ${pol['global']['daily_usd']:g}")
    return " | ".join(parts) or "none (unlimited)"


def collect(days=7):
    """Everything the dashboard and `status` show, as plain data."""
    runtime = load_json(config.DATA / "runtime.json", None)
    now = time.time()
    audit = read_audit(days)
    by_cap, by_user, by_day = (collections.defaultdict(lambda: {"n": 0, "usd": 0.0, "tokens": 0}) for _ in range(3))
    for r in audit:
        usd, tk = r.get("usd") or 0.0, sum((r.get("tokens") or {}).values())
        for bucket, key in ((by_cap, r.get("capability")), (by_user, r.get("user")), (by_day, (r.get("ts") or "")[:10])):
            if key and (usd or tk or r.get("decision") in ("answered", "done")):
                bucket[key]["n"] += 1; bucket[key]["usd"] += usd; bucket[key]["tokens"] += tk
    spend = [r for r in audit if r.get("usd")]
    return {"now": now, "running": bool(runtime) and now - runtime.get("updated_at", 0) <= STALE_S,
            "runtime": runtime, "last_seen_s": (now - runtime["updated_at"]) if runtime else None,
            "days": days, "by_capability": dict(by_cap), "by_user": dict(by_user), "by_day": dict(sorted(by_day.items())),
            "top": sorted(spend, key=lambda r: -r["usd"])[:10], "recent": audit[-20:][::-1]}


def cmd_status(a):
    d = collect(1)
    rt = d["runtime"]
    if not d["running"]:
        print("Dex does not seem to be running" + (f" (last seen {age(d['last_seen_s'])} ago)." if rt else " (no runtime.json yet)."))
        return 1
    pol = rt["policy"]
    print(f"Dex pid {rt['pid']}, up {age(d['now'] - rt['started_at'])} (updated {age(d['last_seen_s'])} ago)")
    print("State: " + ("PAUSED (no new costly work)" if pol["paused"] else "running") + (f" | switched off: {', '.join(pol['disabled'])}" if pol["disabled"] else ""))
    print("Caps: " + caps_line(pol))
    print(f"Today: ${pol['global_usd']:.2f}, {tok(pol.get('tokens'))}, {pol['router_calls']} router calls")
    print(f"\nRunning now ({len(rt['running'])}):")
    for j in rt["running"]:
        print(f"  {j['kind']:8s} {str(j.get('user') or '-'):12s} {j['what']} {('@' + j['sha']) if j['sha'] else ''} {j['status']} {age(j['age_s'])}, {j['steps']} steps, {tok(j['tokens'])} - {j['activity']}")
    if not rt["running"]:
        print("  nothing")
    print(f"\nRecently finished ({len(rt['recent'])}):")
    for j in rt["recent"][:8]:
        print(f"  {j['kind']:8s} {str(j.get('user') or '-'):12s} {j['what']} {j['status']} {j.get('outcome') or ''} {tok(j['tokens'])}")
    return 0


def cmd_usage(a):
    pol = (load_json(config.DATA / "runtime.json", None) or {}).get("policy") or {}
    u = load_json(config.DATA / "usage.json", {})
    print(f"Today ({u.get('date', '?')}): total ${u.get('global_usd', 0):.2f}, {tok(u.get('tokens'))}")
    for user, r in sorted((u.get("users") or {}).items(), key=lambda kv: -kv[1].get("usd", 0)):
        counts = ", ".join(f"{k} {v}" for k, v in r.get("counts", {}).items()) or "no counted requests"
        print(f"  {user:14s} ${r.get('usd', 0):6.2f}  {tok(r.get('tokens')):58s} {counts}")
    d = collect(a.days)
    print(f"\nLast {a.days} days (from the audit log):")
    for title, key in (("by day", "by_day"), ("by capability", "by_capability"), ("by person", "by_user")):
        print(f"  {title}:")
        for k, v in sorted(d[key].items(), key=lambda kv: (kv[0] if key == "by_day" else -kv[1]["usd"])):
            print(f"    {k:14s} ${v['usd']:7.2f}  {human(v['tokens']):>7s} tokens  {v['n']} actions")
    return 0


def cmd_top(a):
    rows = collect(a.days)["top"][:a.n] if a.n <= 10 else sorted([r for r in read_audit(a.days) if r.get("usd")], key=lambda r: -r["usd"])[:a.n]
    print(f"Most expensive actions, last {a.days} days:")
    for r in rows:
        print(f"  ${r['usd']:6.2f}  {r.get('ts', '')[:16]}  {r.get('capability', ''):9s} {r.get('decision', ''):14s} {str(r.get('user')):12s} {r.get('pr') or r.get('repo') or ''}  {tok(r.get('tokens')) if r.get('tokens') else ''}")
    return 0 if rows else 1


def cmd_show(a):
    cfg = load_json(POLICY, {})
    print(f"paused: {bool(cfg.get('paused'))}\ndisabled: {', '.join(cfg.get('disabled') or []) or 'none'}")
    print(f"caps: {caps_line({'max_job_usd': cfg.get('max_job_usd'), 'max_pr_files': cfg.get('max_pr_files'), 'global': cfg.get('global')})}")
    print(f"policy file: {POLICY}" + ("" if POLICY.exists() else " (does not exist: everything allowed, unlimited)"))
    return 0


def _amount(text):
    if text == "off":
        return None
    try:
        v = float(text)
    except ValueError:
        raise ValueError(f"not an amount: {text!r} (a number, or 'off')")
    if v <= 0:
        raise ValueError("the amount must be above zero (use 'off' to remove a cap)")
    return v


def _caps(names):
    bad = [n for n in names if n not in ALL]
    if bad:
        raise ValueError(f"unknown capability {bad[0]!r}; known: {', '.join(ALL)}")
    return names


def _set(cfg, path, value):
    for k in path[:-1]:
        cfg = cfg.setdefault(k, {})
    if value is None:
        cfg.pop(path[-1], None)
    else:
        cfg[path[-1]] = value


def cmd_change(a):
    def mutate(cfg):
        if a.cmd == "pause":
            cfg["paused"] = True
        elif a.cmd == "resume":
            cfg["paused"] = False
        elif a.cmd in ("disable", "enable"):
            names = _caps(a.capabilities)
            cur = set(cfg.get("disabled") or [])
            cfg["disabled"] = sorted(cur | set(names) if a.cmd == "disable" else cur - set(names))
        elif a.cmd == "cap":
            if a.what == "job-usd":
                which = ["review", "question"] if a.target == "all" else _caps([a.target])
                if not isinstance(cfg.get("max_job_usd"), dict):
                    cfg["max_job_usd"] = {}
                for c in which:
                    _set(cfg, ["max_job_usd", c], _amount(a.value))
                if not cfg["max_job_usd"]:
                    del cfg["max_job_usd"]
            elif a.what == "daily-usd":
                _set(cfg, ["global", "daily_usd"], _amount(a.target))
            elif a.what == "user-usd":
                _set(cfg, ["roles", a.target, "daily_usd"], _amount(a.value))
            elif a.what == "pr-files":
                v = _amount(a.target)
                _set(cfg, ["max_pr_files"], None if v is None else int(v))
    edit_file(POLICY, mutate)
    return cmd_show(a)


def cmd_serve(a):
    from . import dashboard
    dashboard.serve(a.port)
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="dexctl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status"); sub.add_parser("show"); sub.add_parser("pause"); sub.add_parser("resume")
    u = sub.add_parser("usage"); u.add_argument("--days", type=int, default=7)
    t = sub.add_parser("top"); t.add_argument("--n", type=int, default=10); t.add_argument("--days", type=int, default=7)
    for name in ("disable", "enable"):
        sub.add_parser(name).add_argument("capabilities", nargs="+")
    c = sub.add_parser("cap"); cs = c.add_subparsers(dest="what", required=True)
    j = cs.add_parser("job-usd"); j.add_argument("target"); j.add_argument("value")
    d = cs.add_parser("daily-usd"); d.add_argument("target")
    r = cs.add_parser("user-usd"); r.add_argument("target"); r.add_argument("value")
    p = cs.add_parser("pr-files"); p.add_argument("target")
    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=8787)
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    handlers = {"status": cmd_status, "usage": cmd_usage, "top": cmd_top, "show": cmd_show, "serve": cmd_serve}
    try:
        return handlers[a.cmd](a) if a.cmd in handlers else cmd_change(a)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
