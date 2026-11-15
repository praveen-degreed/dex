"""Import first in every test module: points the bot at a throwaway data dir and dummy settings."""
import json, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = tempfile.mkdtemp(prefix="botdata-")
os.environ.update(
    BOT_DATA_DIR=DATA, OWNER_USER_ID="UOWNER", SLACK_BOT_TOKEN="xoxb-test", SLACK_APP_TOKEN="xapp-test",
    REPOS=json.dumps({"degreed/fe-workspace": {"clone": "/nonexistent/fe", "skill": "/review-prs"},
                      "degreed/degreed-coach-builder": {"clone": "/nonexistent/cb", "skill": "/review-prs-be"}}),
)
os.environ.pop("ALLOWED_USERS", None)
