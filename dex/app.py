"""Startup: connect to Slack, register the event handler, and run the housekeeping thread."""
import logging
import os
import threading
import time

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from . import config, control, handlers, qa, runtime, services, skills
from . import slack_utils as su

log = logging.getLogger("dex")


def housekeeping(interval=1800):
    """Expire idle thread sessions."""
    while True:
        try:
            removed = services.sessions.expire()
            if removed:
                log.info("expired %d idle thread session(s)", removed)
        except Exception as err:
            log.warning("housekeeping failed: %s", err)
        time.sleep(interval)


def main():
    config.setup_logging()
    skills.ensure(config, log)  # install missing review skills; disable repos that cannot be reviewed (never stops the bot)
    app = App(token=os.environ["SLACK_BOT_TOKEN"])
    su.init(app.client, own_id=app.client.auth_test().get("user_id"))
    app.event({"type": "message"})(handlers.handle)
    app.event("app_mention")(handlers.handle)
    threading.Thread(target=housekeeping, daemon=True).start()
    threading.Thread(target=runtime.write_loop, daemon=True, name="runtime-snapshot").start()  # for dexctl and the dashboard
    threading.Thread(target=qa.refresh_loop, daemon=True, name="git-fetch").start()  # keeps origin/main fresh in the existing clones
    threading.Thread(target=control.loop, daemon=True, name="control").start()  # stop / run / restart from the dashboard
    print("dex listening (reviews, questions, status, stop, usage)")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
