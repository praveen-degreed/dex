"""Who is who: Slack user id -> name and GitHub login, from people.json (edit freely; reloaded when it changes).

people.json: {"U0EXAMPLE01": {"name": "Ada Lovelace", "github": "ada-l"}, ...}
Used so a question like "what is <@U0AJ...> doing?" can be answered from that person's GitHub work.
"""
import json
import threading

from . import config

_lock = threading.Lock()
_cache = {"mtime": None, "data": {}}


def _load():
    path = config.DATA / "people.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    with _lock:
        if mtime != _cache["mtime"]:
            try:
                _cache["data"] = json.loads(path.read_text()) if mtime is not None else {}
            except ValueError:
                _cache["data"] = {}
            _cache["mtime"] = mtime
        return _cache["data"]


def get(slack_id):
    return _load().get(slack_id)


def all_people():
    return dict(_load())


def known_text():
    rows = [f"- {p.get('name', sid)}: Slack <@{sid}>, GitHub login `{p.get('github', 'unknown')}`"
            for sid, p in all_people().items()]
    return "\n".join(rows) or "(no people configured)"
