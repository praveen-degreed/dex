"""Slack helpers. `client` is set once at startup (app.init) and replaced by a fake in tests."""
import re

from . import people

client = None
bot_id = None  # the bot's own Slack user id, set at startup


def init(slack_client, own_id=None):
    global client, bot_id
    client, bot_id = slack_client, own_id


def dm(owner, text):
    client.chat_postMessage(channel=owner, text=text)


def reply(channel, thread_ts, text):
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)


def react(channel, ts, name):
    try:
        client.reactions_add(channel=channel, timestamp=ts, name=name)
    except Exception:
        pass  # cosmetic (already reacted, missing scope, ...)


def react_remove(channel, ts, name):
    try:
        client.reactions_remove(channel=channel, timestamp=ts, name=name)
    except Exception:
        pass


def thread_info(channel, root):
    """What the router needs to know about a thread: its first message and the bot's last reply."""
    try:
        msgs = client.conversations_replies(channel=channel, ts=root, limit=100).get("messages") or []
    except Exception:
        return {"parent_text": "", "last_bot": None}
    last_bot = next((m.get("text", "") for m in reversed(msgs) if m.get("bot_id")), None)
    return {"parent_text": (msgs[0].get("text", "") if msgs else ""), "last_bot": (last_bot or "")[:400] or None}


def strip_mentions(text):
    return re.sub(r"<@[A-Z0-9]+>", "", text or "").strip()


def to_slack(text):
    """Convert common Markdown to Slack mrkdwn, drop a repo-CLAUDE.md marker line, and cap the length."""
    text = re.sub(r"^\s*\S?\s*repository ingested\s*$", "", text, flags=re.M | re.I)
    text = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", text, flags=re.M)             # headings -> bold
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)                           # **bold** -> *bold*
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", text)    # [t](u) -> <u|t>
    text = text.strip()
    return text if len(text) <= 3500 else text[:3450].rstrip() + "\n…(truncated)"


def render_mentions(text, owner=None):
    """Turn Slack mentions into readable names so the model can tell who is meant.

    Mentions of the bot (and of the owner, who is how people address the bot) at the very start of the
    message are dropped: they address the bot, they are not the subject. Other mentions become the person's
    name and GitHub login when known (people.json), else stay as <@ID>.
    """
    addressed = "|".join(re.escape(i) for i in (bot_id, owner) if i)
    if addressed:
        text = re.sub(rf"^\s*(?:<@(?:{addressed})(?:\|[^>]*)?>\s*[,:]?\s*)+", "", text or "")

    def sub(m):
        uid = m.group(1)
        if uid == bot_id:
            return ""
        p = people.get(uid)
        if not p:
            return f"<@{uid}>"
        return f"{p.get('name', uid)} (GitHub `{p['github']}`)" if p.get("github") else p.get("name", uid)

    return re.sub(r"<@([A-Z0-9]+)(?:\|[^>]*)?>", sub, text or "").strip()
