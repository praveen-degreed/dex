"""What a running review is doing, read from Claude's stream events, and the text of a status reply."""
import json
import time

from . import state

# What a Bash command in the stream means, first match wins (checked against the command text).
ACTIVITY = [
    ("pr-worktree.sh create", "creating a worktree at the PR head"),
    ("fetch-pr.sh", "fetching the PR"),
    ("triage.sh", "planning the review"),
    ("split-pr.sh", "splitting a large PR into units"),
    ("gh pr review", "posting the review"),
    ("pr-worktree.sh cleanup", "cleaning up"),
    ("cleanup-pr.sh", "cleaning up"),
    ("gh pr checks", "reading CI status"),
    ("gh pr diff", "reading the diff"),
    ("gh pr view", "reading the PR"),
    ("gh api", "reading earlier reviews"),
]


def describe(ev):
    """Short description of what a stream event shows Claude doing, or None if it shows nothing new."""
    if ev.get("type") != "assistant":
        return None
    msg = ev.get("message")
    for b in (msg.get("content") if isinstance(msg, dict) else None) or []:
        if not isinstance(b, dict) or b.get("type") != "tool_use":
            continue
        name, inp = b.get("name"), b.get("input") or {}
        if name == "Bash":
            cmd = inp.get("command", "")
            return next((text for needle, text in ACTIVITY if needle in cmd), "running a command")
        if name in ("Agent", "Task"):
            return f"running reviewer {inp.get('subagent_type') or inp.get('description') or 'agent'}"
        if name == "Skill":
            return "loading the review skill"
        if name in ("Edit", "Write"):
            return "writing the review"
        return "reading the code"  # Read, Grep, Glob, ...
    return None


LOG_KEEP = 60  # tool calls kept per job for the dashboard


def _short(text, n=140):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def calls(ev):
    """The tool calls in an assistant event, as (id, tool, detail, from_main_session) for the dashboard's live log."""
    if ev.get("type") != "assistant":
        return []
    msg = ev.get("message")
    out = []
    for b in (msg.get("content") if isinstance(msg, dict) else None) or []:
        if not isinstance(b, dict) or b.get("type") != "tool_use":
            continue
        name, inp = b.get("name") or "?", b.get("input") or {}
        detail = (inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("description")
                  or inp.get("subagent_type") or inp.get("skill") or "")
        out.append((b.get("id"), name, _short(detail), ev.get("parent_tool_use_id") is None))
    return out


def results(ev):
    """tool_use ids answered in a user event (a finished tool call, including a finished review agent)."""
    if ev.get("type") != "user":
        return []
    msg = ev.get("message")
    return [b.get("tool_use_id") for b in (msg.get("content") if isinstance(msg, dict) else None) or []
            if isinstance(b, dict) and b.get("type") == "tool_result"]


def track(key, line):
    """Parse one stream line; if `key` names a job, update its progress. Returns the event, or None if not JSON."""
    try:
        ev = json.loads(line)
    except ValueError:
        return None
    if key is not None:
        what = describe(ev)
        with state.lock:
            job = state.jobs.get(key)
            if job:
                job["last_event"] = time.time()
                if what:
                    job["activity"] = what
                    job["steps"] += 1
                now = time.time()
                log, agents = job.setdefault("log", []), job.setdefault("agents", [])
                for cid, name, detail, main in calls(ev):
                    log.append({"t": now, "tool": name, "detail": detail, "main": main})
                    if main and name in ("Agent", "Task"):
                        agents.append({"id": cid, "name": detail or "agent", "status": "running", "started": now})
                del log[:-LOG_KEEP]
                for rid in results(ev):
                    for a in agents:
                        if a["id"] == rid and a["status"] == "running":
                            a.update(status="done", finished=now)
        msg = ev.get("message") if ev.get("type") == "assistant" else None
        u = msg.get("usage") if isinstance(msg, dict) else None
        if u and msg.get("id"):  # outside the lock: add_usage takes it
            state.add_usage(key, msg["id"], (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0),
                            u.get("cache_read_input_tokens") or 0, u.get("output_tokens") or 0, main=ev.get("parent_tool_use_id") is None)
    return ev


def ago(secs):
    m, s = divmod(max(int(secs), 0), 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"


def status_text(key):
    with state.lock:
        job = dict(state.jobs[key])
        ahead = [j for j in state.jobs.values() if j["status"] in ("queued", "running") and j["queued_at"] < job["queued_at"]]
    now = time.time()
    head = job.get("label") or f"{job['url']} @ `{job['sha'][:7]}`"
    if job["status"] == "queued":
        return f"{head} is queued ({len(ahead)} ahead of it); it has not started yet."
    if job["status"] == "running":
        return (f"{head} has been running for {ago(now - job['started_at'])}. Now: {job['activity']}. "
                f"{job['steps']} steps so far, the last one {ago(now - job['last_event'])} ago.")
    took = f" in {ago(job['finished_at'] - job['started_at'])}" if job.get("started_at") else ""
    if job["status"] == "done":
        return f"{head} finished {ago(now - job['finished_at'])} ago{took}: {job['outcome']}. {job.get('link', '')}".strip()
    if job["status"] == "stopped":
        return f"{head} was stopped {ago(now - job['finished_at'])} ago."
    return f"{head} failed {ago(now - job['finished_at'])} ago."


def overview_text():
    with state.lock:
        live = sorted((k for k, j in state.jobs.items() if j["status"] in ("queued", "running")),
                      key=lambda k: state.jobs[k]["queued_at"])
    return "\n".join(status_text(k) for k in live) or "Nothing is running or queued."
