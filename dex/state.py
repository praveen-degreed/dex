"""Shared runtime state: what is running, which threads belong to which job, and duplicate-event guards.

All of it is in memory except `seen` (claimed PR@commit pairs), which is saved so a restart does not re-review.
Always take `lock` before touching these structures.
"""
import json
import threading
import time

from . import config

lock = threading.Lock()
seen = set(json.loads(config.STATE.read_text())) if config.STATE.exists() else set()
inflight = set()      # review keys currently queued or running
jobs = {}             # review key -> progress record (url, sha, repo, status, steps, activity, ...)
threads = {}          # (channel, thread_ts) -> review key, for review threads
handled = set()       # (channel, ts) of events already acted on, so message + app_mention do not both act
procs = {}            # (channel, thread_ts) -> running claude subprocess, so `stop` can kill it
stopped = set()       # (channel, thread_ts) whose process was killed on purpose
cancelled = set()     # review keys cancelled while still queued
qa_active = {}        # (channel, thread_ts) -> start time of a running answer
msg_usage = {}        # job key -> {message id: (fresh input, cache read, output, from the main session)}, counted once per model call
usage_adjust = {}     # job key -> tokens the stream under-reported, added once the final usage is known


def claim(key, force):
    """Atomically claim a PR@sha. Returns False if already reviewed (and not forced)."""
    with lock:
        if key in seen and not force:
            return False
        seen.add(key)
        config.STATE.write_text(json.dumps(sorted(seen)))
        return True


def release(key):
    with lock:
        seen.discard(key)
        config.STATE.write_text(json.dumps(sorted(seen)))


def handled_before(channel, ts):
    """True if this Slack event was already handled (a message can arrive as both message and app_mention)."""
    with lock:
        if (channel, ts) in handled:
            return True
        handled.add((channel, ts))
        if len(handled) > 5000:
            handled.clear()
        return False


def new_job(key, **fields):
    with lock:
        inflight.add(key)
        jobs[key] = {"kind": "review", "status": "queued", "queued_at": time.time(), "steps": 0, "activity": "waiting to start",
                     "last_event": time.time(), "tokens": {"fresh": 0, "cache_read": 0, "output": 0}, **fields}


def update_job(key, **fields):
    with lock:
        if key in jobs:
            jobs[key].update(fields)


def _recount(key):
    job, per, adj = jobs.get(key), msg_usage.get(key, {}), usage_adjust.get(key, (0, 0, 0))
    if job is not None:
        job["tokens"] = {"fresh": sum(v[0] for v in per.values()) + adj[0], "cache_read": sum(v[1] for v in per.values()) + adj[1],
                         "output": sum(v[2] for v in per.values()) + adj[2]}


def add_usage(key, mid, fresh, cache_read, output, main=True):
    """Record one model call's tokens for a job (a message id seen again keeps its largest figures)."""
    with lock:
        if key not in jobs:
            return
        per = msg_usage.setdefault(key, {})
        old = per.get(mid, (0, 0, 0, main))
        per[mid] = (max(old[0], fresh), max(old[1], cache_read), max(old[2], output), main)
        _recount(key)


def reconcile(key, usage):
    """The stream reports a message's output tokens before they are final. When the run ends, Claude gives the final
    usage of the main session: add whatever the stream missed (sub-agent calls are left as streamed)."""
    with lock:
        if key not in jobs or not usage:
            return
        main = [v for v in msg_usage.get(key, {}).values() if v[3]]
        final = ((usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0),
                 usage.get("cache_read_input_tokens") or 0, usage.get("output_tokens") or 0)
        usage_adjust[key] = tuple(max(0, final[i] - sum(v[i] for v in main)) for i in range(3))
        _recount(key)


def finish_job(key):
    """Drop the in-flight mark and keep only the 50 most recent finished jobs."""
    with lock:
        inflight.discard(key)
        msg_usage.pop(key, None)
        usage_adjust.pop(key, None)
        for k in [k for k, j in jobs.items() if j["status"] in ("done", "failed", "stopped")][:-50]:
            jobs.pop(k, None)


def stop_thread(channel, thread_ts):
    """Stop whatever runs in this thread. Returns 'running', 'queued' or None."""
    k = (channel, thread_ts)
    with lock:
        proc = procs.get(k)
        if proc is not None and proc.poll() is None:
            stopped.add(k)
            proc.kill()
            return "running"
        job_key = threads.get(k)
        if job_key and jobs.get(job_key, {}).get("status") == "queued":
            cancelled.add(job_key)
            return "queued"
    return None
