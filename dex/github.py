"""GitHub helpers (through the `gh` CLI)."""
import datetime
import json
import re
import subprocess

PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
REVIEW_LABEL = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes requested", "COMMENTED": "comments posted"}


def gh_json(args):
    r = subprocess.run(["gh"] + args, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip()[:200] or f"gh exited {r.returncode}")
    return json.loads(r.stdout)


def pr_info(url):
    """{'sha','state','is_fork'} for a PR URL. Raises RuntimeError if gh cannot read it."""
    r = subprocess.run(
        ["gh", "pr", "view", url, "--json", "headRefOid,state,isCrossRepository,changedFiles",
         "-q", '.headRefOid+" "+.state+" "+(.isCrossRepository|tostring)+" "+(.changedFiles|tostring)'],
        capture_output=True, text=True,
    )
    if r.returncode:
        raise RuntimeError(r.stderr.strip()[:300] or "gh pr view failed")
    sha, state, fork, files = r.stdout.split()
    return {"sha": sha, "state": state, "is_fork": fork == "true", "files": int(files)}


def pr_paths(url):
    """The changed file paths of a PR (gh returns at most the first 100). Raises RuntimeError if gh cannot read it."""
    r = subprocess.run(["gh", "pr", "view", url, "--json", "files", "-q", ".files[].path"], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip()[:300] or "gh pr view failed")
    return [p for p in r.stdout.splitlines() if p]


def posted_review(repo, number, since):
    """(state, url) of the newest review by the gh user on the PR submitted since `since`, else None."""
    try:
        me = subprocess.run(["gh", "api", "user", "-q", ".login"], capture_output=True, text=True).stdout.strip()
        reviews = gh_json(["api", f"repos/{repo}/pulls/{number}/reviews?per_page=100"])
    except Exception:
        return None
    floor = since - datetime.timedelta(seconds=60)  # tolerate clock skew with GitHub
    mine = [
        r for r in reviews
        if (r.get("user") or {}).get("login") == me and r.get("submitted_at")
        and datetime.datetime.fromisoformat(r["submitted_at"].replace("Z", "+00:00")) >= floor
    ]
    if not mine:
        return None
    r = mine[-1]
    return r.get("state", ""), r.get("html_url", "")
