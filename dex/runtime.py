"""A live snapshot of what Dex is doing, written to runtime.json every few seconds.

`dexctl status` and the dashboard read this file from their own processes, so they never touch the bot's memory
and a crash in either cannot affect it.
"""
import json
import os
import time

from . import config, control, services, state

STARTED = time.time()
PATH = config.DATA / "runtime.json"


def _row(key, j, now):
    return {"id": key, "kind": j.get("kind", "review"), "user": j.get("user"), "what": j.get("label") or j.get("url"),
            "sha": (j.get("sha") or "")[:7], "status": j.get("status"), "activity": j.get("activity"), "steps": j.get("steps", 0),
            "tokens": dict(j.get("tokens") or {}), "outcome": j.get("outcome"),
            "age_s": round(now - (j.get("started_at") or j.get("queued_at") or now), 1),
            # for the dashboard's job view
            "url": j.get("url") or "", "repo": j.get("repo") or "", "label": j.get("label") or "", "link": j.get("link") or "",
            "channel": j.get("channel"), "thread": j.get("thread"), "queued_at": j.get("queued_at"),
            "started_at": j.get("started_at"), "finished_at": j.get("finished_at"),
            "log": [dict(e) for e in (j.get("log") or [])[-40:]],
            "agents": [{k: a.get(k) for k in ("name", "status", "started", "finished")} for a in (j.get("agents") or [])]}


def snapshot():
    now = time.time()
    with state.lock:
        live = [_row(k, j, now) for k, j in state.jobs.items() if j["status"] in ("queued", "running")]
        recent = sorted(((j.get("finished_at") or 0, _row(k, j, now)) for k, j in state.jobs.items() if j["status"] in ("done", "failed", "stopped")),
                        key=lambda t: -t[0])[:15]
    return {"pid": os.getpid(), "started_at": STARTED, "updated_at": now, "running": live, "recent": [r for _, r in recent],
            "policy": services.policy.snapshot(), "control": control.status()}


def write_once(path=None):
    path = path or PATH
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot()))
    tmp.replace(path)  # atomic: a reader never sees half a file


def write_loop(interval=3):
    while True:
        try:
            write_once()
        except Exception:
            pass  # a snapshot failing must never affect the bot
        time.sleep(interval)
