"""Slack event routing: decide whether a message is for the bot, what it means, and who may ask for it."""
import logging
import re
import time

from . import config, conversation, ctl, progress, reviews, router, services, state
from .policy import edit_file
from . import slack_utils as su
from .github import PR_URL

log = logging.getLogger("dex")

# Only used when the LLM router fails, so the bot keeps working with simple rules.
STATUS_WORDS = re.compile(r"\b(status|progress|updates?|eta|how long|done yet|finished yet|what'?s (happening|going on))\b", re.I)
FORCE = re.compile(r"\bforce\b", re.I)

CAPABILITY = {"review": "review", "agent": "question", "status": "status", "stop": "stop", "usage": "usage", "control": "usage"}
MAX_LINKS = int(__import__("os").environ.get("MAX_LINKS_PER_MESSAGE", "5"))  # PRs reviewed per message
LOW_CONFIDENCE = 0.5  # below this the router is guessing: hand the message to the assistant, which can ask


def fallback_verdict(text, mentions, tracked):
    """Rules used only when the router is unavailable."""
    has_url = bool(PR_URL.search(text))
    if mentions and has_url and not STATUS_WORDS.search(text):
        return {"intent": "review", "directed_at_bot": True, "force": bool(FORCE.search(text)), "confidence": 1.0}
    if STATUS_WORDS.search(text) and (tracked or mentions):
        return {"intent": "status", "directed_at_bot": True, "force": False, "confidence": 1.0}
    if mentions:
        return {"intent": "agent", "directed_at_bot": True, "force": False, "confidence": 1.0}
    return {"intent": "ignore", "directed_at_bot": False, "force": False, "confidence": 1.0}


def answer_status(text, channel, thread_ts, review_key):
    """Reply with the progress of this thread's review or answer, of a PR named in the message, or an overview."""
    k = (channel, thread_ts)
    if review_key and review_key in state.jobs:
        return su.reply(channel, thread_ts, progress.status_text(review_key))
    with state.lock:
        started = state.qa_active.get(k)
    if started:
        return su.reply(channel, thread_ts, f"Working on your question now ({progress.ago(time.time() - started)} so far).")
    m = PR_URL.search(text)
    if m:
        with state.lock:
            matches = [key for key, j in state.jobs.items() if j["url"].lower() == m.group(0).lower()]
        if matches:
            return su.reply(channel, thread_ts, progress.status_text(max(matches, key=lambda x: state.jobs[x]["queued_at"])))
        return su.reply(channel, thread_ts, f"I have no review of {m.group(0)} since I last started. Mention me with the link to start one.")
    return su.reply(channel, thread_ts, progress.overview_text())


def do_stop(channel, thread_ts):
    what = state.stop_thread(channel, thread_ts)
    if what == "running":
        return su.reply(channel, thread_ts, "Stopping it now.")
    if what == "queued":
        return su.reply(channel, thread_ts, "Cancelled; it hadn't started yet.")
    return su.reply(channel, thread_ts, "Nothing is running in this thread.")


def known_jobs(text):
    """Reviews the bot has (or had) for the PR links in a message, so the router can tell a status question about
    one of them from a question about some other PR."""
    urls = {m.group(0).lower() for m in PR_URL.finditer(text)}
    with state.lock:
        return [{"url": j["url"], "status": j.get("status")} for j in state.jobs.values() if j.get("url", "").lower() in urls]


def switches_text():
    pol = services.policy.snapshot()
    return (f"Dex is {'PAUSED (no new costly work)' if pol['paused'] else 'running'}. "
            f"Switched off: {', '.join(pol['disabled']) or 'nothing'}. Caps: {ctl.caps_line(pol)}.")


def do_control(channel, thread_ts, user, action):
    """Pause, resume or show the switches. Owner only, whatever the router decided."""
    if user != config.OWNER:
        services.policy.audit(user=user, capability="control", channel=channel, thread=thread_ts, decision="denied")
        return su.reply(channel, thread_ts, "Only the owner can pause Dex or change its switches.")
    if action in ("pause", "resume"):
        edit_file(services.policy.path, lambda c: c.update(paused=(action == "pause")))
        services.policy.reload()
        services.policy.audit(user=user, capability="control", channel=channel, thread=thread_ts, decision=action)
    return su.reply(channel, thread_ts, switches_text() + (" Use `dexctl` for the other switches." if action != "pause" else " Say `resume` to start again."))


def build_context(text, mentions_bot, mentions_owner, review_key, session, info):
    with state.lock:
        job = state.jobs.get(review_key) if review_key else None
    return {
        "mentions_bot": mentions_bot, "mentions_owner": mentions_owner,
        "pr_urls_in_message": [m.group(0) for m in PR_URL.finditer(text)],
        "known_review_jobs": known_jobs(text),
        "thread": {
            "is_review_thread": bool(review_key), "review_status": (job or {}).get("status", "none"),
            "has_conversation": bool(session),
            "parent_pr_url": (lambda m: m.group(0) if m else None)(PR_URL.search(info.get("parent_text", ""))),
            "last_bot_message": info.get("last_bot"),
        },
    }


def handle(event):
    user, text = event.get("user"), event.get("text", "")
    # Never log message text, only metadata and the reason for the decision.
    meta = f"type={event.get('type')} channel={event.get('channel')} user={user}"
    if event.get("bot_id") or event.get("subtype") or not user:
        return log.info("skip (bot/subtype message) %s subtype=%s", meta, event.get("subtype"))
    services.policy.maybe_reload()
    channel, msg_ts, root = event["channel"], event["ts"], event.get("thread_ts")
    thread_ts = root or msg_ts
    mentions_bot = event.get("type") == "app_mention"
    mentions_owner = f"<@{config.OWNER}>" in text
    review_key = state.threads.get((channel, root)) if root else None
    session = services.sessions.get(channel, root) if root else None
    tracked = bool(review_key or session)

    # Structural checks only (not intent): is this message for the bot at all?
    if not (mentions_bot or mentions_owner or tracked):
        return log.info("skip (not addressed to the bot) %s", meta)
    if user == config.OWNER and not (mentions_bot or tracked):
        return log.info("skip (own message) %s", meta)  # the owner's plain messages, unless in a bot thread
    if config.ALLOWED and user not in config.ALLOWED and user != config.OWNER:
        return log.info("skip (user not in ALLOWED_USERS) %s", meta)
    # Only now mark the event handled, so a skipped message cannot swallow its app_mention twin.
    if state.handled_before(channel, msg_ts):
        return log.info("skip (already handled) %s", meta)

    info = su.thread_info(channel, root) if root else {"parent_text": "", "last_bot": None}
    verdict, via = None, "router"
    if services.policy.router_call_allowed():
        verdict = router.classify(text, build_context(text, mentions_bot, mentions_owner, review_key, session, info), policy=services.policy)
    if verdict is None:
        verdict, via = fallback_verdict(text, mentions_bot or mentions_owner, tracked), "rules"
    else:
        services.policy.add_cost(user, verdict.get("cost", 0.0))
    intent = verdict["intent"]
    log.info("route intent=%s conf=%.2f directed=%s via=%s %s", intent, verdict["confidence"], verdict["directed_at_bot"], via, meta)
    if intent == "ignore" and mentions_bot:  # a direct @-mention is for the bot by definition: never leave it unanswered
        verdict, intent = {**verdict, "intent": "agent"}, "agent"  # a copy: never change the router's answer in place
    if intent == "ignore" or not (mentions_bot or mentions_owner or verdict["directed_at_bot"]):
        return
    if verdict["confidence"] < LOW_CONFIDENCE and via == "router" and intent != "agent":
        log.info("low confidence (%.2f) on %s: handing to the assistant", verdict["confidence"], intent)
        verdict, intent = {**verdict, "intent": "agent"}, "agent"  # a copy: never change the router's answer in place

    ok, why = services.policy.check(user, CAPABILITY[intent])
    if not ok:
        services.policy.audit(user=user, capability=CAPABILITY[intent], channel=channel, thread=thread_ts, decision="denied")
        return su.reply(channel, thread_ts, why)

    if intent == "review":
        found = list(PR_URL.finditer(text)) or list(PR_URL.finditer(info.get("parent_text", "")))
        matches = list({m.group(0).lower(): m for m in found}.values())  # one per distinct PR, in the order given
        if not matches:
            return su.reply(channel, thread_ts, "Which PR should I review? Send me the link.")
        force = verdict["force"] or bool(FORCE.search(text))
        if len(matches) > MAX_LINKS:
            su.reply(channel, thread_ts, f"You sent {len(matches)} PRs; I take up to {MAX_LINKS} per message. "
                     f"Reviewing the first {MAX_LINKS}; send the rest in another message.")
            matches = matches[:MAX_LINKS]
        elif len(matches) > 1:
            su.reply(channel, thread_ts, f"{len(matches)} PRs: reviewing them one at a time, in the order you sent them.")
        for m in matches:
            reviews.start_review(channel, thread_ts, msg_ts, user, m, force=force)
        return
    if intent == "status":
        return answer_status(text, channel, thread_ts, review_key)
    if intent == "stop":
        return do_stop(channel, thread_ts)
    if intent == "usage":
        return su.reply(channel, thread_ts, services.policy.summary(user, everyone=(user == config.OWNER)))
    if intent == "control":
        return do_control(channel, thread_ts, user, verdict.get("action", ""))
    if intent == "agent":
        return conversation.start_question(channel, thread_ts, msg_ts, user, text, verdict, review_key, session,
                                           info.get("parent_text", ""))
