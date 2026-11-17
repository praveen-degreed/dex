"""Commands from the dashboard to the running bot: stop or cancel a job, start a review, restart.

The dashboard is its own process and never touches the bot's memory. It drops one JSON file per command into
control/inbox/ (`submit`); a thread in the bot (`loop`) picks each one up within a second, acts through the same
functions Slack uses, and writes the outcome to control/results/<id>.json for the page to show (`result`).
Settings (pause, switches, caps, Claude accounts) do not come through here: the dashboard edits policy.json the way
dexctl does, and the bot picks that up by itself.

Anyone who can write to the data folder can already edit policy.json, so the inbox adds no new way in. A command
older than MAX_AGE_S (say, written while the bot was down) is dropped, never acted on late.
"""
import json
import logging
import os
import re
import secrets
import sys
import threading
import time

from . import config, github, services, state
from . import slack_utils as su

log = logging.getLogger("dex")
DIR = config.DATA / "control"
INBOX, RESULTS = DIR / "inbox", DIR / "results"
MAX_AGE_S = 60
KEEP_RESULTS_S = 600
ACTIONS = ("stop", "run", "restart", "cancel_restart")
_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_restart = {"requested_at": None, "thread": None}
_rlock = threading.Lock()


# --- the dashboard's side -------------------------------------------------------------------------------------------
def submit(action, args=None, source="page"):
    """Queue a command for the bot. Returns its id."""
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    INBOX.mkdir(parents=True, exist_ok=True)
    cid = secrets.token_hex(8)
    tmp = INBOX / f".{cid}.tmp"  # not *.json, so the bot never reads half a file
    tmp.write_text(json.dumps({"id": cid, "action": action, "args": args or {}, "ts": time.time(), "source": source}))
    tmp.replace(INBOX / f"{cid}.json")
    return cid


def result(cid, wait=0.0):
    """The bot's answer to a command ({"ok", "message", ...}), waiting up to `wait` seconds; None if there is none yet."""
    if not _ID.match(cid or ""):
        return None
    path, end = RESULTS / f"{cid}.json", time.time() + wait
    while True:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            pass
        if time.time() >= end:
            return None
        time.sleep(0.2)


# --- the bot's side -------------------------------------------------------------------------------------------------
def status():
    """For runtime.json: is a restart waiting for the running work to finish."""
    with _rlock:
        return {"restart_pending": _restart["requested_at"] is not None, "restart_requested_at": _restart["requested_at"]}


def _busy():
    with state.lock:
        return sum(1 for j in state.jobs.values() if j["status"] in ("queued", "running")) + len(state.qa_active)


def _thread_of(key):
    with state.lock:
        j = state.jobs.get(key) or {}
        if j.get("channel") and j.get("thread"):
            return j["channel"], j["thread"]
        return next((k for k, v in state.threads.items() if v == key), None)


def do_stop(args):
    key = str(args.get("job") or "")
    with state.lock:
        job = dict(state.jobs.get(key) or {})
    if not job:
        return False, "Dex no longer knows that job."
    if job["status"] not in ("queued", "running"):
        return False, f"It has already {job['status']}."
    where = _thread_of(key)
    if not where:
        return False, "Dex cannot find the Slack thread of that job."
    what = state.stop_thread(*where)
    if what is None:
        return False, "It is still getting ready (fetching the PR); try again in a few seconds."
    try:
        su.reply(where[0], where[1], f"<@{config.OWNER}> stopped this from the Dex page.")
    except Exception:
        pass
    return True, "Stopping it now." if what == "running" else "Cancelled; it had not started."


def do_run(args):
    from . import reviews  # late: reviews pulls in the whole review flow
    url = str(args.get("url") or "").strip()
    m = github.PR_URL.match(url)
    if not m or len(url) > 300:
        return False, "That is not a GitHub pull request link."
    force = bool(args.get("force"))
    channel = os.environ.get("DEX_PAGE_CHANNEL") or config.OWNER
    posted = su.client.chat_postMessage(channel=channel, text=f"<@{config.OWNER}> asked for a review from the Dex page: {m.group(0)}"
                                        + (" (force: rerun even if this commit was reviewed)" if force else ""))
    ch, ts = posted["channel"], posted["ts"]
    services.policy.maybe_reload()
    reviews.start_review(ch, ts, ts, config.OWNER, m, force)
    with state.lock:
        key = state.threads.get((ch, ts))
    if key:
        return True, "Review queued. Dex replies in its Slack thread.", {"job": key}
    return False, "Dex did not start it; its reason is in the Slack thread."


def _drain_then_restart():
    while True:
        with _rlock:
            if _restart["requested_at"] is None:
                _restart["thread"] = None
                return
        if not _busy():
            services.policy.audit(user=config.OWNER, capability="control", decision="restarted", source="page")
            log.info("restarting (asked from the dashboard)")
            logging.shutdown()
            os.execv(sys.executable, [sys.executable, "-u"] + sys.argv)  # same process id, so tmux / caffeinate keep it
        time.sleep(2)


def do_restart(args):
    with _rlock:
        if _restart["requested_at"] is None:
            _restart["requested_at"] = time.time()
        if _restart["thread"] is None:
            _restart["thread"] = threading.Thread(target=_drain_then_restart, daemon=True, name="restart")
            _restart["thread"].start()
    n = _busy()
    return True, f"Restarting after {n} running job{'s' if n != 1 else ''} finish." if n else "Restarting now."


def do_cancel_restart(args):
    with _rlock:
        was = _restart["requested_at"] is not None
        _restart["requested_at"] = None
    return was, "Restart cancelled." if was else "No restart was waiting."


HANDLERS = {"stop": do_stop, "run": do_run, "restart": do_restart, "cancel_restart": do_cancel_restart}


def _write_result(cid, ok, message, extra=None):
    RESULTS.mkdir(parents=True, exist_ok=True)
    tmp = RESULTS / f".{cid}.tmp"
    tmp.write_text(json.dumps({"id": cid, "ok": ok, "message": message, "at": time.time(), **(extra or {})}))
    tmp.replace(RESULTS / f"{cid}.json")


def process_once(now=None):
    """Act on every command in the inbox. Returns how many were read."""
    now = now or time.time()
    n = 0
    for f in sorted(INBOX.glob("*.json")) if INBOX.exists() else []:
        n += 1
        try:
            cmd = json.loads(f.read_text())
        except (OSError, ValueError):
            cmd = None
        f.unlink(missing_ok=True)
        if not isinstance(cmd, dict) or cmd.get("id") != f.stem or not _ID.match(f.stem):
            continue
        action, args = cmd.get("action"), cmd.get("args") if isinstance(cmd.get("args"), dict) else {}
        if action not in HANDLERS:
            _write_result(f.stem, False, "Unknown action.")
            continue
        if not isinstance(cmd.get("ts"), (int, float)) or now - cmd["ts"] > MAX_AGE_S:
            _write_result(f.stem, False, "Expired: Dex was not running when it was sent.")
            continue
        try:
            out = HANDLERS[action](args)
        except Exception as err:
            log.warning("control %s failed: %s", action, err)
            out = (False, f"Failed: {str(err)[:200]}")
        ok, message, extra = out[0], out[1], (out[2] if len(out) > 2 else None)
        _write_result(f.stem, ok, message, extra)
        services.policy.audit(user=config.OWNER, capability="control", decision=f"page_{action}", source=cmd.get("source") or "page",
                              ok=ok, job=args.get("job"), pr=args.get("url"))
    for f in RESULTS.glob("*.json") if RESULTS.exists() else []:
        try:
            if now - f.stat().st_mtime > KEEP_RESULTS_S:
                f.unlink(missing_ok=True)
        except OSError:
            pass
    return n


def loop(interval=1.0):
    while True:
        try:
            process_once()
        except Exception as err:
            log.warning("control loop: %s", err)  # a bad command must never take the bot down
        time.sleep(interval)
