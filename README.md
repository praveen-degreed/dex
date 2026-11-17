# Dex

A Slack agent (Socket Mode, runs locally). People mention the bot, or tag the owner, and it:

- **reviews a PR** (`fe-workspace` via `/review-prs`; `degreed-coach-builder` and `degreed-assistant` via `/review-prs-be`); the skill posts its own decision to GitHub. A message with several PR links reviews each distinct one, one at a time (up to `MAX_LINKS_PER_MESSAGE`, default 5); a bare link, or "review" in a thread whose parent is a PR link, is enough;
- **answers anything else it can find out** ("who changed this and why?", "what is Shubham working on?", "my open PRs") as an assistant with read-only access to both repos' `main`, git history and GitHub. A Slack thread is one Claude session, so follow-ups keep their context, and the assistant **asks a clarifying question** when it needs one;
- reports **status**, **stops** a running job, and shows **usage**.

No command format is needed: an LLM router (`dex/router.py`, a small tool-less `claude -p --bare` call) decides whether a message is one of four fixed actions (review, status, stop, usage) or goes to the assistant. It picks a fixed action only when the message clearly asks for it; everything else, and anything it is unsure about, goes to the assistant. If the router fails, simple rules keep reviews and status working. Nothing acts on a label alone: PR links are extracted by regex and checked against the configured repos.

## Layout
```
bot.py                 entry point: python -u bot.py
bin/botq               read-only helper the assistant runs (code, history, people's PRs, org-wide search)
bin/dexctl             see what Dex is doing and change its switches
people.json            who is who (Slack id -> name, GitHub login): edit freely
readonly_repos.json    repos the assistant may read and search but never review (e.g. the monolith)
repos_extra.json       more reviewable repos (same shape as REPOS in .env), so .env stays untouched; see repos_extra.example.json
dex/
  config.py            settings, paths, logging (BOT_DATA_DIR relocates all runtime data)
  tools.py             tool allowlists / deny lists for the claude runs  <- the safety boundary
  github.py            gh helpers, PR link parsing
  slack_utils.py       send / react / mrkdwn formatting / thread info
  state.py             shared runtime state: jobs, threads, procs, claims, dedupe
  progress.py          live-step tracking and status text
  claude_runner.py     streaming `claude -p` runner with timeout and stop
  router.py            LLM intent router
  policy.py            roles, switches (pause/disable), caps, usage and token tracking, audit log
  runtime.py           live snapshot (runtime.json) for dexctl and the dashboard
  ctl.py               the dexctl command
  dashboard.py         the read-only local dashboard
  qa.py                thread-session store, read-only checkouts, the assistant's prompt
  people.py            Slack id -> name + GitHub login (people.json)
  tools_cli.py         the assistant's read-only helper: `bin/botq activity|prs <login>`, `find <keywords>` (org-wide PR search)
  reviews.py           the PR review flow
  conversation.py      the question flow
  handlers.py          Slack event routing
  app.py               startup wiring
  services.py          shared policy and session-store instances
tests/                 test_policy, test_core, test_flow, test_tools_cli, test_botq_code, test_ctl (unit, no network) and eval_router (real model calls)
legacy/                the previous single-file bot, kept for reference
```

## Setup
1. Slack app: Socket Mode on. Bot scopes `app_mentions:read`, `chat:write`, `channels:history`, `groups:history`, `reactions:write`; events `message.channels`, `message.groups`, `app_mention`. Invite the bot to the channels.
2. `cp env.example .env` and fill in `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `OWNER_USER_ID`, `REPOS`; copy `people.example.json` to `people.json` and `readonly_repos.example.json` to `readonly_repos.json` (both optional)
   (`{"degreed/fe-workspace":{"clone":"/path","skill":"/review-prs"},"degreed/degreed-coach-builder":{"clone":"/path","skill":"/review-prs-be"}}`).
   More reviewable repos (for example `degreed/degreed-assistant` with `/review-prs-be`) can go in `repos_extra.json` instead of `.env` (see `repos_extra.example.json`).
   Optional: `MAX_LINKS_PER_MESSAGE` (5), `JOB_TIMEOUT_MIN` (45), `QA_TIMEOUT_MIN` (10), `MAX_QA_TURNS_PER_THREAD` (15), `CONCURRENCY` (1), `QA_CONCURRENCY` (2), `QA_BARE` (1), `QA_IDLE_HOURS` (24), `ALLOWED_USERS` (empty = anyone).
3. `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`; `gh auth status` and `claude` must work.
4. Run: `tmux new -s dex` then `caffeinate -i .venv/bin/python -u bot.py`. Code changes load only on a restart (tmux does not reload code or restart a crashed bot). **Do not restart while a review runs: it kills it** (check `pgrep -fl 'claude.* -p'` first). A killed review leaves nothing posted; send the link again with `force`.
5. The review skills are installed from GitHub `main` (`install.sh --global` in `fe-claude-skills` and `be-claude-skills`), not from local files: a skill change takes effect only after it is merged and reinstalled. Each review reads the installed skill fresh, so no Dex restart is needed for that.

## Seeing and controlling spend (`dexctl` and the dashboard)
```
bin/dexctl status                  what is running now (live steps and tokens), the switches and caps
bin/dexctl usage [--days N]        spend and tokens: today per person, and the last N days
bin/dexctl top [--n N]             the most expensive actions
bin/dexctl pause | resume          stop / allow all costly work (status, stop, usage still answer)
bin/dexctl disable review          switch a capability off for everyone (enable to undo)
bin/dexctl cap job-usd review 4    stop any single review that spends more than $4 (soft cap, see below)
bin/dexctl cap daily-usd 10        the whole bot's spend per day
bin/dexctl cap pr-files 80         refuse reviews of PRs with more than 80 changed files (the owner can force)
bin/dexctl serve [--port 8787]     a read-only dashboard at http://127.0.0.1:8787
```
Everything is unlimited by default. The commands edit `policy.json` (atomically) and Dex picks the change up on its next
message, with no restart. In Slack the owner can say `@Dex pause` / `@Dex resume` / `@Dex what is switched off?`; anyone
else is refused. Live token counts come from Claude's stream (fresh input, cached input, output) and are recorded per
person and in `audit.jsonl`.

*Per-job caps use the Claude CLI's `--max-budget-usd`, which is checked between model turns: a job can overshoot a cap by
up to one turn (a large review turn is about $0.05-0.20). A job that hits its cap is stopped, nothing is posted, and the
asker and the owner are told.* The dashboard runs as its own process, binds to 127.0.0.1 only, answers GET only, rejects
any other `Host` header (so a web page cannot reach it), and changes nothing.

## What the assistant can see
Read-only checkouts of `main` for every repo in `REPOS` (reviewable) and `readonly_repos.json` (read-only), built as `git worktree`s of your clones under `qa/` (they share git history; your working copies, branches and uncommitted work are never read). A background thread creates and refreshes them every 10 minutes, all repos in parallel, so a huge first checkout never blocks an answer; a repo that is not ready yet is listed to the assistant as such. GitHub questions ("is anyone working on X") search the whole `GITHUB_ORG` (default `degreed`) with `bin/botq find`, and every answer states where it looked.

## When a review is not approved
The skill approves only a complete review. Common reasons for a comment-only review: a reviewer ran out of turns, a step was denied, the PR is your own (GitHub does not allow approving it), or the review ran without its agents. Look at the posted `Not verified` section, then at the kept log in `drafts/<repo>-<pr>-<sha>.jsonl` (`dexctl` and the DM list denials). Cost scales with the PR: a small PR should be well under $2, a huge one can reach $8; cap a single review with `bin/dexctl cap job-usd review N`.

## Policy: who may do what, and limits
With no `policy.json`, **everyone may do everything, unlimited**; usage is still recorded. Copy `policy.example.json` to `policy.json` to add roles and limits (per person, per capability, per day, optional dollar budgets); edits apply without a restart. `@bot usage` shows your usage; the owner sees everyone's. Every decision and action is appended to `audit.jsonl`.

## Safety
- Reviews run in a throwaway worktree with a private temp dir; the only write access is that temp dir. `gh pr merge`, `git push`, `git commit` and any `../` path are denied. A review never runs the PR's own code (no `pytest`, `ruff`): test results come from CI.
- The review allowlist (`dex/tools.py`) is strict on purpose: the skills' scripts are allowed as `.../scripts/*` (the `:*` form never matched), a chained command is allowed only if every part is allowed, shell variables are denied, and the review is posted with `--body-file` (an inline multi-line body with a `#` line is denied). Denied calls are counted per review and DM'd to the owner. Raw event logs of reviews that were denied something, failed, or were comment-only are kept in `drafts/` to see why.
- Review prompts and skills are untrusted-input aware: PR text is data, never instructions. Full permission bypass is not used.
- The assistant is read-only: it reads only the repo checkouts (not the bot's `.env`), runs git only as `git -C <checkout> <read-only subcommand>`, and uses GitHub only for PR and search lookups. Edit, push, commit and merge are denied.
- Slack messages, PR text, logs and code are treated as untrusted data by every prompt.

## Tests
```
.venv/bin/python tests/test_policy.py && .venv/bin/python tests/test_core.py && .venv/bin/python tests/test_flow.py && .venv/bin/python tests/test_tools_cli.py && .venv/bin/python tests/test_botq_code.py && .venv/bin/python tests/test_ctl.py
.venv/bin/python tests/eval_router.py     # real model calls (~$0.05)
tests/check_permissions.py   # real model calls (~$0.9): every skill script and read command is allowed, merge/push/rm/inline-body are not.
                             # Run it after editing dex/tools.py or updating a review skill.
```
