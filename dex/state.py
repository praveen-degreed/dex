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
        jobs[key] = {"status": "queued", "queued_at": time.time(), "steps": 0, "activity": "waiting to start",
                     "last_event": time.time(), **fields}


def update_job(key, **fields):
    with lock:
        if key in jobs:
            jobs[key].update(fields)


def finish_job(key):
    """Drop the in-flight mark and keep only the 50 most recent finished jobs."""
    with lock:
        inflight.discard(key)
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
