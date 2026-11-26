# Development

Contributions come in as pull requests: see [`../CONTRIBUTING.md`](../CONTRIBUTING.md).

## Tests
```
make test                                     # nine unit-test files, no network, no cost
.venv/bin/python tests/eval_router.py         # costs money: real model calls (~$0.05)
.venv/bin/python tests/check_permissions.py   # costs money: real model calls (~$1 to $3): every skill script and read
                                              # command is allowed, merge/push/rm/inline-body are not. Run it after editing
                                              # dex/tools.py or updating a review skill.
```

## Layout
```
bot.py                 entry point: python -u bot.py (make run)
Makefile               make setup | doctor | run | test | skills
bin/botq               read-only helper the assistant runs (code, history, people's PRs, org-wide search)
bin/dexctl             see what Dex is doing and change its switches
slack-app-manifest.yml the Slack app, ready to paste
dex/
  config.py            settings, paths, logging (BOT_DATA_DIR relocates all runtime data)
  tools.py             tool allowlists / deny lists for the claude runs  <- the safety boundary
  github.py            gh helpers, PR link parsing
  slack_utils.py       send / react / mrkdwn formatting / thread info
  state.py             shared runtime state: jobs, threads, procs, claims, dedupe
  progress.py          live-step tracking and status text
  claude_runner.py     streaming `claude -p` runner with timeout and stop
  claude_env.py        which Claude account each kind of run uses, and its environment
  router.py            LLM intent router
  policy.py            roles, switches (pause/disable), caps, usage and token tracking, audit log
  runtime.py           live snapshot (runtime.json) for dexctl and the dashboard
  ctl.py               the dexctl command
  dashboard.py         the local Dex page (dashboard.html): status, controls, settings
  control.py           the bot's inbox for the page's stop / run / restart commands
  skills.py            installs the review skills the configured repos need; checks each repo is reviewable
  qa.py                thread-session store, read-only checkouts, the assistant's prompt
  people.py            Slack id -> name + GitHub login (people.json)
  tools_cli.py         the assistant's read-only helper: `bin/botq activity|prs <login>`, `find <keywords>`
  reviews.py           the PR review flow
  conversation.py      the question flow
  handlers.py          Slack event routing
  app.py               startup wiring
  services.py          shared policy and session-store instances
tests/                 unit tests (no network) and eval_router / check_permissions (real model calls)
docs/                  these pages, plus open-source-plan.md
```
