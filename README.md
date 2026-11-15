# Dex

A Slack agent (Socket Mode, runs locally). People mention the bot, or tag the owner, and it:

- **reviews a PR** (`fe-workspace` via `/review-prs`, `degreed-coach-builder` via `/review-prs-be`); the skill posts its own decision to GitHub;
- **answers anything else it can find out** ("who changed this and why?", "what is Shubham working on?", "my open PRs") as an assistant with read-only access to both repos' `main`, git history and GitHub. A Slack thread is one Claude session, so follow-ups keep their context, and the assistant **asks a clarifying question** when it needs one;
- reports **status**, **stops** a running job, and shows **usage**.

An LLM router (`dex/router.py`, a small tool-less `claude -p --bare` call) decides whether a message is one of four fixed actions (review, status, stop, usage) or goes to the assistant. It picks a fixed action only when the message clearly asks for it; everything else, and anything it is unsure about, goes to the assistant. If the router fails, simple rules keep reviews and status working. Nothing acts on a label alone: PR links are extracted by regex and checked against the configured repos.

## Layout
```
bot.py                 entry point: python -u bot.py
bin/botq               read-only helper the assistant runs (people's PRs and activity)
people.json            who is who (Slack id -> name, GitHub login): edit freely
readonly_repos.json    repos the assistant may read and search but never review (e.g. the monolith)
dex/
  config.py            settings, paths, logging (BOT_DATA_DIR relocates all runtime data)
  tools.py             tool allowlists / deny lists for the claude runs  <- the safety boundary
  github.py            gh helpers, PR link parsing
  slack_utils.py       send / react / mrkdwn formatting / thread info
  state.py             shared runtime state: jobs, threads, procs, claims, dedupe
  progress.py          live-step tracking and status text
  claude_runner.py     streaming `claude -p` runner with timeout and stop
  router.py            LLM intent router
  policy.py            roles, configurable limits, usage tracking, audit log
  qa.py                thread-session store, read-only checkouts, the assistant's prompt
  people.py            Slack id -> name + GitHub login (people.json)
  tools_cli.py         the assistant's read-only helper: `bin/botq activity|prs <login>`, `find <keywords>` (org-wide PR search)
  reviews.py           the PR review flow
  conversation.py      the question flow
  handlers.py          Slack event routing
  app.py               startup wiring
  services.py          shared policy and session-store instances
tests/                 test_policy, test_core, test_flow, test_tools_cli (unit, no network) and eval_router (real model calls)
legacy/                the previous single-file bot, kept for reference
```

## Setup
1. Slack app: Socket Mode on. Bot scopes `app_mentions:read`, `chat:write`, `channels:history`, `groups:history`, `reactions:write`; events `message.channels`, `message.groups`, `app_mention`. Invite the bot to the channels.
2. `cp env.example .env` and fill in `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `OWNER_USER_ID`, `REPOS`; copy `people.example.json` to `people.json` and `readonly_repos.example.json` to `readonly_repos.json` (both optional)
   (`{"degreed/fe-workspace":{"clone":"/path","skill":"/review-prs"},"degreed/degreed-coach-builder":{"clone":"/path","skill":"/review-prs-be"}}`).
   Optional: `JOB_TIMEOUT_MIN` (45), `QA_TIMEOUT_MIN` (10), `MAX_QA_TURNS_PER_THREAD` (15), `CONCURRENCY` (1), `QA_CONCURRENCY` (2), `QA_BARE` (1), `QA_IDLE_HOURS` (24), `ALLOWED_USERS` (empty = anyone).
3. `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`; `gh auth status` and `claude` must work.
4. Run: `tmux new -s dex` then `caffeinate -i .venv/bin/python -u bot.py`. Do not restart while a review runs: it kills it.

## What the assistant can see
Read-only checkouts of `main` for every repo in `REPOS` (reviewable) and `readonly_repos.json` (read-only), built as `git worktree`s of your clones under `qa/` (they share git history; your working copies, branches and uncommitted work are never read). A background thread creates and refreshes them every 10 minutes, all repos in parallel, so a huge first checkout never blocks an answer; a repo that is not ready yet is listed to the assistant as such. GitHub questions ("is anyone working on X") search the whole `GITHUB_ORG` (default `degreed`) with `bin/botq find`, and every answer states where it looked.

## Policy: who may do what, and limits
With no `policy.json`, **everyone may do everything, unlimited**; usage is still recorded. Copy `policy.example.json` to `policy.json` to add roles and limits (per person, per capability, per day, optional dollar budgets); edits apply without a restart. `@bot usage` shows your usage; the owner sees everyone's. Every decision and action is appended to `audit.jsonl`.

## Safety
- Reviews run in a throwaway worktree with a private temp dir; the only write access is that temp dir. `gh pr merge`, `git push`, `git commit` are denied.
- The assistant is read-only: it reads only the repo checkouts (not the bot's `.env`), runs git only as `git -C <checkout> <read-only subcommand>`, and uses GitHub only for PR and search lookups. Edit, push, commit and merge are denied.
- Slack messages, PR text, logs and code are treated as untrusted data by every prompt.

## Tests
```
.venv/bin/python tests/test_policy.py && .venv/bin/python tests/test_core.py && .venv/bin/python tests/test_flow.py && .venv/bin/python tests/test_tools_cli.py
.venv/bin/python tests/eval_router.py     # real model calls (~$0.05)
```
