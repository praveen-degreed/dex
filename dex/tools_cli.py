"""Read-only helper commands the assistant runs instead of hand-writing GitHub queries.

    botq activity <github-login> [--days N]                 what a person is working on, in every configured repo
    botq prs <github-login> [--state open|merged|all] [--days N]   a person's pull requests
    botq find <keywords...> [--state open|merged|all] [--days N]   PRs across the WHOLE GitHub org, by repo and author

Code and history, read straight from the existing clones (the paths in REPOS) at origin/main (never the working tree, never a
local branch, never uncommitted or unpushed work; nothing is checked out or copied):
    botq ls <repo> [path] [-r]                      botq show <repo> <path> [--from N] [--to M]
    botq grep <repo> <pattern> [--path P] [-i] [--regex]
    botq log <repo> [--path P] [--search S] [--grep G] [--author A] [--since YYYY-MM-DD] [--max N]
    botq blame <repo> <path> --from N --to M        botq commit <repo> <sha> [--path P]

Every argument is validated and passed to `gh` as an argument list (never through a shell). Output is compact text.
"""
import argparse
import datetime
import json
import re
import subprocess
import sys

from . import config

LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
TERM = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ .:/#-]{0,59}$")  # a search term: never starts with "-", so it cannot be a flag
LIMIT = 100


def gh_pr_list(repo, *extra):
    r = subprocess.run(["gh", "pr", "list", "--repo", repo, "--limit", str(LIMIT), *extra], capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError(r.stderr.strip()[:200] or "gh failed")
    return json.loads(r.stdout or "[]")


def capped(prs):
    """A note when gh returned exactly the limit, so a total is never reported as if it were complete."""
    return [f"  (list capped at {LIMIT}; there may be more)"] if len(prs) >= LIMIT else []


def since(days):
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


def line(pr, extra=""):
    flag = "[draft] " if pr.get("isDraft") else ""
    return f"  #{pr['number']} {flag}{pr['title']}{extra} | {pr['url']}"


def open_prs(repo, login, days=None):
    """Open PRs by `login`. With `days`, PRs updated in that window are listed and older ones are summarised."""
    prs = gh_pr_list(repo, "--author", login, "--state", "open", "--json", "number,title,isDraft,reviewDecision,updatedAt,url")
    cutoff = since(days) if days else None
    recent = [p for p in prs if not cutoff or p["updatedAt"][:10] >= cutoff]
    old = [p for p in prs if cutoff and p["updatedAt"][:10] < cutoff]
    rows = [line(p, f" | review={p.get('reviewDecision') or 'none'} | updated {p['updatedAt'][:10]}") for p in recent]
    if old:
        drafts = sum(1 for p in old if p.get("isDraft"))
        rows.append(f"  + {len(old)} older open PRs not updated since before {cutoff} ({drafts} drafts; "
                    f"least recently updated {min(p['updatedAt'] for p in old)[:10]})")
    return rows + capped(prs)


def merged_prs(repo, login, days):
    prs = gh_pr_list(repo, "--author", login, "--state", "merged", "--search", f"merged:>={since(days)}", "--json", "number,title,mergedAt,url")
    return [line(p, f" | merged {p['mergedAt'][:10]}") for p in prs] + capped(prs)


def waiting_on(repo, login):
    """Open PRs where `login` was asked to review, updated in the last 30 days (older ones are auto-requests nobody opened)."""
    prs = gh_pr_list(repo, "--state", "open", "--search", f"review-requested:{login} updated:>={since(30)}", "--json", "number,title,author,isDraft,url")
    return [line(p, f" | by {(p.get('author') or {}).get('login', '?')}") for p in prs] + capped(prs)


def section(title, rows):
    n = sum(1 for r in rows if r.startswith("  #"))
    return [f"{title} ({n}):"] + (rows or ["  none"])


def activity(login, days):
    out = [f"Activity for GitHub user `{login}` (GitHub work only; last {days} days for merged work)"]
    for repo in config.REPOS:
        out.append(f"\n== {repo} ==")
        for title, fn in ((f"Open PRs updated in the last {days} days", lambda: open_prs(repo, login, days)), (f"Merged in the last {days} days", lambda: merged_prs(repo, login, days)),
                          ("Waiting on their review (updated in the last 30 days)", lambda: waiting_on(repo, login))):
            try:
                out += section(title, fn())
            except Exception as e:
                out.append(f"{title}: could not read ({e})")
    return "\n".join(out)


def prs(login, state, days):
    out = []
    for repo in config.REPOS:
        out.append(f"== {repo} ==")
        if state in ("open", "all"):
            out += section("Open", open_prs(repo, login))
        if state in ("merged", "all"):
            out += section(f"Merged in the last {days} days", merged_prs(repo, login, days))
    return "\n".join(out)


def find(keywords, state, days, show=25):
    """Search PRs across the whole org. The header always says where it looked, so an answer cannot overstate its scope."""
    if not config.GITHUB_ORG:
        return "Org-wide search is off: GITHUB_ORG is not set in .env."
    cmd = ["gh", "search", "prs", "--owner", config.GITHUB_ORG, "--limit", str(LIMIT), "--sort", "updated", "--order", "desc",
           "--json", "repository,number,title,author,isDraft,updatedAt,state,url"]
    if state == "open":
        cmd += ["--state", "open"]
    elif state == "merged":
        cmd += ["--merged"]
    if days:
        cmd += ["--updated", f">={since(days)}"]
    cmd += ["--", " ".join(keywords)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if r.returncode:
        raise RuntimeError(r.stderr.strip()[:200] or "gh search failed")
    hits = json.loads(r.stdout or "[]")
    scope = f"Searched {state} PRs across the whole `{config.GITHUB_ORG}` GitHub org for: {' '.join(keywords)}" + (f" (updated in the last {days} days)" if days else "")
    if not hits:
        return f"{scope}\nNo results. (Search needs every word to match: try fewer or different terms.)"
    by_repo, by_author = {}, {}
    for h in hits:
        by_repo[h["repository"]["nameWithOwner"]] = by_repo.get(h["repository"]["nameWithOwner"], 0) + 1
        a = (h.get("author") or {}).get("login", "?")
        by_author[a] = by_author.get(a, 0) + 1
    rank = lambda d: ", ".join(f"{k} {v}" for k, v in sorted(d.items(), key=lambda kv: -kv[1]))
    out = [scope, f"{len(hits)}{'+' if len(hits) >= LIMIT else ''} result(s). By repo: {rank(by_repo)}", f"By author: {rank(by_author)}",
           f"Newest first (showing {min(show, len(hits))}):"]
    for h in hits[:show]:
        out.append(f"  {h['repository']['nameWithOwner']}#{h['number']} {'[draft] ' if h.get('isDraft') else ''}{h['title']} | "
                   f"{(h.get('author') or {}).get('login', '?')} | {h.get('state', '').lower()} | updated {h['updatedAt'][:10]} | {h['url']}")
    return "\n".join(out + capped(hits))


# --- code and history, from the clones' git objects at origin/<default branch> -------------------------------------

PATH_RE = re.compile(r"^[A-Za-z0-9_@+=,~ ./*?\[\]-]{1,300}$")
SHA_RE = re.compile(r"^[0-9a-f]{4,40}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AUTHOR_RE = re.compile(r"^[\w.@ -]{1,60}$")
MAX_BLOB = 1_000_000


def resolve_repo(name):
    n = (name or "").strip().lower()
    for key, cfg in config.ALL_REPOS.items():
        if n in (key, key.split("/")[-1]):
            return key, cfg
    raise ValueError(f"unknown repo '{name}'; known repos: {', '.join(k.split('/')[-1] for k in config.ALL_REPOS)}")


def ref_of(cfg):
    return f"origin/{cfg.get('default_branch', 'main')}"


def git(cfg, *args, timeout=60):
    return subprocess.run(["git", "-C", cfg["clone"], *args], capture_output=True, timeout=timeout)


def text_of(r):
    return r.stdout.decode("utf-8", "replace")


def clean_path(p, glob=False):
    """A repo-relative path or pathspec. No absolute paths, no `..`, no leading `-` (a flag) or `:` (pathspec magic)."""
    if not PATH_RE.match(p or "") or p.startswith(("-", "/", ":")) or ".." in p.split("/"):
        raise ValueError(f"invalid path: {p!r}")
    if not glob and any(c in p for c in "*?[]"):
        raise ValueError("wildcards are not allowed in this path")
    return p


def clip(text, limit, what="lines"):
    lines = text.splitlines()
    shown = [l if len(l) <= 240 else l[:237] + "..." for l in lines[:limit]]
    if len(lines) > limit:
        shown.append(f"...({len(lines) - limit} more {what} not shown; narrow the request)")
    return "\n".join(shown)


def cmd_ls(a):
    key, cfg = resolve_repo(a.repo)
    ref, args = ref_of(cfg), ["ls-tree", "--name-only"] + (["-r"] if a.recursive else [])
    args += [ref] + (["--", clean_path(a.path).rstrip("/") + "/"] if a.path else [])
    r = git(cfg, *args)
    if r.returncode:
        raise RuntimeError(r.stderr.decode()[:200] or "path not found")
    return f"{key} @ {ref}: {a.path or '/'}\n" + (clip(text_of(r), 200, "entries") or "(empty)")


def cmd_show(a):
    key, cfg = resolve_repo(a.repo)
    blob = f"{ref_of(cfg)}:{clean_path(a.path)}"
    size = git(cfg, "cat-file", "-s", blob)
    if size.returncode:
        raise RuntimeError(f"{a.path} not found on {ref_of(cfg)}")
    if int(size.stdout) > MAX_BLOB:
        raise RuntimeError(f"file is {int(size.stdout)} bytes; too large to show (limit {MAX_BLOB})")
    data = git(cfg, "show", blob).stdout
    if b"\0" in data[:8000]:
        raise RuntimeError("binary file")
    lines = data.decode("utf-8", "replace").splitlines()
    start = max(a.start, 1)
    end = min(a.end or start + 199, start + 299, len(lines))
    body = "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(start, end + 1))
    return f"{key} @ {ref_of(cfg)}: {a.path} lines {start}-{end} of {len(lines)}\n{clip(body, 300)}"


def cmd_grep(a):
    key, cfg = resolve_repo(a.repo)
    if not a.pattern or len(a.pattern) > 200 or "\n" in a.pattern or "\0" in a.pattern:
        raise ValueError("pattern must be 1-200 characters on one line")
    ref = ref_of(cfg)
    args = ["grep", "-n", "-I", "--no-color"] + (["-i"] if a.ignore_case else []) + (["-E"] if a.regex else ["-F"]) + ["-e", a.pattern, ref]
    if a.path:
        args += ["--", clean_path(a.path, glob=True)]
    r = git(cfg, *args, timeout=90)
    if r.returncode == 1:
        return f"{key} @ {ref}: no matches for {a.pattern!r}"
    if r.returncode:
        raise RuntimeError(r.stderr.decode()[:200] or "git grep failed")
    out = "\n".join(l[len(ref) + 1:] if l.startswith(ref + ":") else l for l in text_of(r).splitlines())
    return f"{key} @ {ref}: matches for {a.pattern!r}\n{clip(out, 80, 'matches')}"


def cmd_log(a):
    key, cfg = resolve_repo(a.repo)
    ref, n = ref_of(cfg), max(1, min(a.max, 40))
    args = ["log", ref, "--date=short", "--format=%h %ad %an %s", f"-n{n}"]
    for val, flag, ok in ((a.search, "-S", lambda v: 0 < len(v) <= 100 and "\n" not in v), (a.grep, "--grep=", lambda v: 0 < len(v) <= 100 and "\n" not in v)):
        if val:
            if not ok(val):
                raise ValueError("search text must be 1-100 characters on one line")
            args.append(flag + val)
    if a.grep:
        args.append("-i")
    if a.author:
        if not AUTHOR_RE.match(a.author):
            raise ValueError("invalid author")
        args.append("--author=" + a.author)
    if a.since:
        if not DATE_RE.match(a.since):
            raise ValueError("--since must be YYYY-MM-DD")
        args.append("--since=" + a.since)
    if a.path:
        path = clean_path(a.path, glob=True)
        args += (["--follow"] if not any(c in path for c in "*?[]") else []) + ["--", path]
    r = git(cfg, *args)
    if r.returncode:
        raise RuntimeError(r.stderr.decode()[:200] or "git log failed")
    return f"{key} @ {ref}: history (newest first)\n" + (clip(text_of(r), 40, "commits") or "no commits match")


def cmd_blame(a):
    key, cfg = resolve_repo(a.repo)
    if not (1 <= a.start <= a.end and a.end - a.start < 120):
        raise ValueError("--from/--to must be a range of at most 120 lines, starting at 1 or later")
    r = git(cfg, "blame", "-L", f"{a.start},{a.end}", "--date=short", ref_of(cfg), "--", clean_path(a.path))
    if r.returncode:
        raise RuntimeError(r.stderr.decode()[:200] or "git blame failed")
    return f"{key} @ {ref_of(cfg)}: blame {a.path}:{a.start}-{a.end}\n{clip(text_of(r), 120)}"


def cmd_commit(a):
    key, cfg = resolve_repo(a.repo)
    if not SHA_RE.match(a.sha or ""):
        raise ValueError("not a commit sha (4-40 hex characters)")
    ref = ref_of(cfg)
    if git(cfg, "merge-base", "--is-ancestor", a.sha, ref).returncode != 0:
        raise RuntimeError(f"{a.sha} is not a commit on {ref}")  # unpushed or local-only work is never shown
    if a.path:
        r = git(cfg, "show", "--format=", "--patch", a.sha, "--", clean_path(a.path))
        return f"{key}: {a.sha} changes to {a.path}\n{clip(text_of(r), 200)}"
    r = git(cfg, "show", "--stat", "--date=short", "--format=%h %an %ad%n%s%n%n%b", a.sha)
    return f"{key}: commit {a.sha}\n{clip(text_of(r), 70)}"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="botq", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("activity", "prs"):
        p = sub.add_parser(name); p.add_argument("login"); p.add_argument("--days", type=int, default=14)
        if name == "prs":
            p.add_argument("--state", choices=["open", "merged", "all"], default="open")
    ls = sub.add_parser("ls"); ls.add_argument("repo"); ls.add_argument("path", nargs="?", default=""); ls.add_argument("-r", "--recursive", action="store_true")
    sh = sub.add_parser("show"); sh.add_argument("repo"); sh.add_argument("path"); sh.add_argument("--from", dest="start", type=int, default=1); sh.add_argument("--to", dest="end", type=int, default=0)
    gr = sub.add_parser("grep"); gr.add_argument("repo"); gr.add_argument("pattern"); gr.add_argument("--path", default=""); gr.add_argument("-i", "--ignore-case", action="store_true"); gr.add_argument("--regex", action="store_true")
    lg = sub.add_parser("log"); lg.add_argument("repo"); lg.add_argument("--path", default=""); lg.add_argument("--search", default=""); lg.add_argument("--grep", default=""); lg.add_argument("--author", default=""); lg.add_argument("--since", default=""); lg.add_argument("--max", type=int, default=15)
    bl = sub.add_parser("blame"); bl.add_argument("repo"); bl.add_argument("path"); bl.add_argument("--from", dest="start", type=int, required=True); bl.add_argument("--to", dest="end", type=int, required=True)
    cm = sub.add_parser("commit"); cm.add_argument("repo"); cm.add_argument("sha"); cm.add_argument("--path", default="")
    f = sub.add_parser("find"); f.add_argument("keywords", nargs="+"); f.add_argument("--days", type=int, default=0)
    f.add_argument("--state", choices=["open", "merged", "all"], default="open")
    a = ap.parse_args(argv)
    code = {"ls": cmd_ls, "show": cmd_show, "grep": cmd_grep, "log": cmd_log, "blame": cmd_blame, "commit": cmd_commit}
    if a.cmd in code:
        try:
            print(code[a.cmd](a)); return 0
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr); return 2
        except Exception as e:
            print(f"error: {e}", file=sys.stderr); return 1
    if a.cmd == "find":
        if len(a.keywords) > 8 or not all(TERM.match(k) for k in a.keywords):
            print("error: 1-8 plain search terms (letters, digits, space . : / # - _), not starting with '-'", file=sys.stderr); return 2
        if not 0 <= a.days <= 365:
            print("error: --days must be between 0 and 365", file=sys.stderr); return 2
        try:
            print(find(a.keywords, a.state, a.days)); return 0
        except Exception as e:
            print(f"error: {e}", file=sys.stderr); return 1
    if not LOGIN.match(a.login):
        print("error: not a valid GitHub login", file=sys.stderr); return 2
    if not 1 <= a.days <= 90:
        print("error: --days must be between 1 and 90", file=sys.stderr); return 2
    print(activity(a.login, a.days) if a.cmd == "activity" else prs(a.login, a.state, a.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
