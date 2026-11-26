"""Settings, paths and logging. Everything else reads its configuration from here.

Importing this module needs OWNER_USER_ID and REPOS in the environment (normally from .env).
BOT_DATA_DIR relocates all runtime data (state, drafts, sessions, usage, audit): tests use it.
"""
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent.parent
load_dotenv(HERE / ".env")  # never overrides variables that are already set
DATA = Path(os.environ.get("BOT_DATA_DIR") or HERE)

OWNER = os.environ["OWNER_USER_ID"]
# Optional hard filter on who may talk to the bot at all. Empty = anyone. Finer control lives in policy.json.
ALLOWED = set(filter(None, os.environ.get("ALLOWED_USERS", "").split(",")))
# {"owner/repo": {"clone": "/path", "skill": "/review-prs", "default_branch": "main"}}, keys lowercased
REPOS = {k.lower(): v for k, v in json.loads(os.environ.get("REPOS") or "{}").items()}
# More reviewable repos without touching .env: repos_extra.json in the data dir, same shape (not secret, ignored by git).
try:
    _extra = json.loads((DATA / "repos_extra.json").read_text())
except (OSError, ValueError):
    _extra = {}
REPOS.update({k.lower(): v for k, v in _extra.items() if isinstance(v, dict) and v.get("clone") and v.get("skill")} if isinstance(_extra, dict) else {})

BOT_NAME = os.environ.get("BOT_NAME", "Dex")  # how the assistant names itself in its prompts; the Slack name is set in the Slack app
GITHUB_ORG = os.environ.get("GITHUB_ORG", "")  # org-wide searches ("is anyone working on X") run over this org; empty = that search is off


def _readonly_repos():
    """Repos the assistant may read and search but never review: readonly_repos.json (not secret), keys lowercased."""
    try:
        raw = json.loads((DATA / "readonly_repos.json").read_text())
    except (OSError, ValueError):
        return {}
    return {k.lower(): v for k, v in raw.items() if isinstance(v, dict) and v.get("clone")} if isinstance(raw, dict) else {}


ALL_REPOS = {**_readonly_repos(), **REPOS}  # reviewable repos win on a name clash

TIMEOUT_S = int(os.environ.get("JOB_TIMEOUT_MIN", "45")) * 60          # one PR review
QA_TIMEOUT_S = int(os.environ.get("QA_TIMEOUT_MIN", "10")) * 60        # one answer
MAX_QA_TURNS = int(os.environ.get("MAX_QA_TURNS_PER_THREAD", "15"))
CONCURRENCY = int(os.environ.get("CONCURRENCY", "1"))                  # reviews at a time (they are big)
QA_CONCURRENCY = int(os.environ.get("QA_CONCURRENCY", "2"))            # answers at a time

STATE = DATA / "state.json"             # claimed PR@commit pairs
DRAFTS = DATA / "drafts"                # each review's final text (and raw events when something went wrong)
WORKTREES = DATA / "worktrees"          # one per running review
TMPS = DATA / "tmp"                     # a private TMPDIR per run
QA_DIR = DATA / "qa"                    # an empty working folder for the assistant's runs (no repo is ever copied here)
SESSIONS_FILE = DATA / "qa_sessions.json"
LESSONS = DATA / "memory" / "lessons.md"
for _d in (DRAFTS, WORKTREES, TMPS, QA_DIR, LESSONS.parent):
    _d.mkdir(parents=True, exist_ok=True)


def setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

