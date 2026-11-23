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
claude_profiles.json    optional: redefine or add Claude accounts (see "Switching the Claude account"); claude_profiles.example.json
repos_extra.json       more reviewable repos (same shape as REPOS in .env), so .env stays untouched; see repos_extra.example.json
dex/
  config.py            settings, paths, logging (BOT_DATA_DIR relocates all runtime data)
  tools.py             tool allowlists / deny lists for the claude runs  <- the safety boundary
  github.py            gh helpers, PR link parsing
  slack_utils.py       send / react / mrkdwn formatting / thread info
  state.py             shared runtime state: jobs, threads, procs, claims, dedupe
  progress.py          live-step tracking and status text
  claude_runner.py     streaming `claude -p` runner with timeout and stop
  claude_env.py        which Claude account (Azure Foundry or a subscription) each kind of run uses, and its environment
  router.py            LLM intent router
  policy.py            roles, switches (pause/disable), caps, usage and token tracking, audit log
  runtime.py           live snapshot (runtime.json) for dexctl and the dashboard
  ctl.py               the dexctl command
  dashboard.py         the local Dex page (dashboard.html): status, controls, settings
  control.py           the bot's inbox for the page's stop / run / restart commands
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
5. The review skills are installed from GitHub `main` (`install.sh all` from `praveen-degreed/review-skills`, which holds both the FE and the BE skill), not from local files: a skill change takes effect only after it is merged and reinstalled. Each review reads the installed skill fresh, so no Dex restart is needed for that.

## Reviewing the .NET monolith (`degreed/Degreed`)
Dex runs one main skill for it, `/review-prs-net` (from the review-skills repo), which routes to the team's own skills in the repo:
`/dg-maestro-review` when a changed path matches `maestro|aicoach`, otherwise `/dg-pr-review --analysis-only`. Those skills only draft;
a repo entry (in `repos_extra.json`) with `"dex_posts": true` makes Dex run the skill read-only on a worktree of the PR and then **post
the draft itself with the skill's decision**: the skill ends its review with `DEX_VERDICT: approve|request_changes|comment`, and Dex posts it with `--approve`, `--request-changes` or `--comment`. `skip` means nothing was reviewed. If no review agent ran, or the verdict is `skip`, **nothing is posted to GitHub**: Dex only tells the requester in the thread. Draft PRs are reviewed like any other. Dex falls back to a comment on the poster's own PR (GitHub does not allow approving your own). A skill string may carry
`{number}` / `{url}`; the older `"maestro_skill"` / `"maestro_paths"` keys (routing done by Dex itself) still work for repos without such a skill. A PR that changes anything under `.claude/`
is refused (the skills come from the PR itself, so it could rewrite its own reviewer); the owner can force it. See `repos_extra.example.json`.
Not benchmarked yet: the first real run of each skill is the test. The monolith is large (about 35,000 files), so a review worktree takes a while to create.

## Switching the Claude account (Azure Foundry <-> a Claude subscription)
Every `claude` run (reviews, Slack answers, the router) uses the account in the environment Dex was started with: today Azure
Foundry. You can send any kind of run to a personal Claude subscription instead, and back, **without a restart**:
```
bin/dexctl claude show                       which account each kind of run uses
bin/dexctl claude check subscription         is that account usable (runs `claude auth status` with its environment)
bin/dexctl claude use review subscription    reviews on the subscription; question / router / all work too
bin/dexctl claude use all foundry            everything back on Azure Foundry (the default)
bin/dexctl subscription                      shortcut: reviews on the subscription (questions and router unchanged)
bin/dexctl foundry                           shortcut: everything back on Azure Foundry
```
**One-time setup for the subscription profile** (Dex never logs in for you):
1. `! CLAUDE_CONFIG_DIR=~/.claude-subscription claude` then `/login` with the personal account (the default folder; change it in `claude_profiles.json`).
2. Install the review skills into that folder too: `CLAUDE_HOME=~/.claude-subscription ./install.sh all` (from the review-skills repo).
3. `bin/dexctl claude check subscription` should say `loggedIn=True`.

How it works: the `subscription` profile starts `claude` with its own `CLAUDE_CONFIG_DIR` and **without** the Foundry, cloud-provider
and API-key variables, so Claude Code uses its normal login. A personal API key works too (a profile with `set_from_env`: the key
stays in your environment, never in a file; see `claude_profiles.example.json`). The choice is stored in `policy.json`
(`claude_profile`). A run set to a profile that is missing, not logged in or incomplete **fails with a clear message and never
falls back** to the other account. The audit log records the profile of every review.
Caveats: `--bare` skips the login, so on the subscription the router and answers run without it (more startup tokens each);
a subscription has usage limits that Foundry does not; **running private company code through a personal account may break
company policy: check before you switch.**

## What every finished review reports
The thread reply is laid out as a small, clean card (Slack formatting: bold labels, code-styled numbers, a quote bar for the usage):
```
:speech_balloon: @requester Your review of fe-workspace #4482 is done.
*Result*  Comments posted  ·  View the review

> :bar_chart: *Usage*
> *Tokens*  `11.20M`  (920K new · 10.26M cached · 17K output)
> *Cost*  `$8.17`  _API-equivalent estimate, not billed per token_
> *Agents*  `19`      *Time*  `14m 13s`
> *Account*  Personal Claude plan (subscription)
> :no_entry_sign: 1 tool call blocked by safety rules

:speech_balloon: _Addressed the comments? Ask me to review again right here in this thread ..._
```
The icon follows the result (approved, changes requested, comments posted). The last line only appears when there are comments to address, so the re-review stays in the same thread. On the personal plan the cost is labelled an API-equivalent estimate (you are not billed per token); on Azure Foundry it is just the cost. The blocked-calls line
only appears when something was blocked. The account is read when the review starts, so a switch during a run does not change what is reported; the audit log
records the same `profile`, `agents` and `tokens`. The reply also **tags the person who asked for the review** (`@name`), so they are notified in Slack when it is done, failed, or stopped at the cost limit.

## Queue notices
Reviews run `CONCURRENCY` at a time (default 1; currently 2 in `.env`: two reviews in parallel, so each also uses the account's usage allowance side by side). A review that has to wait is announced in its thread, tagging the requester: it is
queued, #N in line, behind which review (and how many more). The moment it is picked up, Dex posts "Your turn: the review of ... is running
now (it waited 12m 04s)". A review that can start at once just says "Review started". Questions and finished reviews do not count as ahead.

## Big PRs: Dex asks first
A PR above 50 files or 3000 changed lines is **not run straight away**. Dex replies in the thread, tagging the requester: the PR is big, a smaller PR
gets a better review and uses far fewer tokens, can it be split? If it can, the requester sends the smaller PRs. If it can't (for example it is tested
as one piece), the requester just says so in the thread (no mention of Dex needed; an LLM reads the answer, not a keyword) and Dex reviews it **fully**:
the prompt tells the skill the PR cannot be split, so the FE skill reviews every unit instead of stopping at its 12-unit limit. "I'll split it" or
"not now" drops the request. Only the requester or the owner can answer, and an unanswered question is forgotten after a day. The owner's `force`
skips the question. Change the limits with `bin/dexctl cap big-pr <files> [lines]`, `off` or `default` (`dexctl show` prints them). A fully reviewed
huge PR is expensive; `dexctl cap job-usd review N` still caps one review.

## When the Claude account reaches its usage limit
If a run fails because the account behind it (Azure Foundry or the personal plan) hit its usage or rate limit, Dex says so instead of a generic
"failed": the thread reply tags the requester ("the personal Claude plan has reached its usage limit, it resets around HH:MM, send it again after the
reset"), the owner gets a DM with what Claude actually said and which `dexctl` command switches account, and the review can be retried on the same
commit. Dex then remembers the limit and **does not start new reviews on that account** until it resets (the message's reset time, or 30 minutes if
it gave none); the owner can still force one. Every raw limit message is appended to `limits.jsonl`. The wording of a real limit message is not
documented, so detection is deliberately broad (usage limit, rate limit, 429, quota, "resets at ..."); it only looks at runs that already failed, and
`limits.jsonl` shows what Claude really said so the patterns can be tightened.

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
bin/dexctl serve [--port 8787]     the Dex page at http://127.0.0.1:8787 (see below)
```
Everything is unlimited by default. The commands edit `policy.json` (atomically) and Dex picks the change up on its next
message, with no restart. In Slack the owner can say `@Dex pause` / `@Dex resume` / `@Dex what is switched off?`; anyone
else is refused. Live token counts come from Claude's stream (fresh input, cached input, output) and are recorded per
person and in `audit.jsonl`.

*Per-job caps use the Claude CLI's `--max-budget-usd`, which is checked between model turns: a job can overshoot a cap by
up to one turn (a large review turn is about $0.05-0.20). A job that hits its cap is stopped, nothing is posted, and the
asker and the owner are told.*

### The Dex page (`bin/dexctl serve`)
One page with three views: **Overview** (paste a PR link to preview and run a review; spend, reviews, questions and
tokens today; live jobs with their steps, current tool call and review agents; recent jobs with Rerun; an activity
feed), **Jobs** (every job since Dex started, with its tool calls, agents and timeline) and **Settings** (switch reviews
or questions off, pick the Claude account per kind of run, set the limits; changes are staged and applied together).
Stop, Cancel and Restart wait 5 seconds with an Undo. Restart waits until nothing is running, then restarts Dex in place
(same process id, so tmux and caffeinate keep it).

How it is safe: the page is its own process, binds to 127.0.0.1 only and rejects any other `Host` header (no DNS
rebinding). Reading needs nothing more; every change also needs the page's own `Origin` and a per-process token sent in a
custom header, so another web page cannot make one. Settings go through the same code as `dexctl` (same validation) into
`policy.json`. Stop, run and restart are files dropped in `control/inbox/` that the bot picks up within a second
(`dex/control.py`); one older than a minute is dropped, never acted on late. Every change is in `audit.jsonl` with
`"source": "page"`. Reviews started from the page are announced in a Slack message (to the owner's DM, or
`DEX_PAGE_CHANNEL`) and run in that thread like any other. Stop, Run and Restart need the bot restarted once with this
version; the page says so until then. Live cost is not known until a run ends (Claude reports it at the end), so a running
job shows tokens and its cap.

## What the assistant can see
Read-only checkouts of `main` for every repo in `REPOS` (reviewable) and `readonly_repos.json` (read-only), built as `git worktree`s of your clones under `qa/` (they share git history; your working copies, branches and uncommitted work are never read). A background thread creates and refreshes them every 10 minutes, all repos in parallel, so a huge first checkout never blocks an answer; a repo that is not ready yet is listed to the assistant as such. GitHub questions ("is anyone working on X") search the whole `GITHUB_ORG` (default `degreed`) with `bin/botq find`, and every answer states where it looked.

## When a review is not approved
The skill approves only a complete review. Common reasons for a comment-only review: a reviewer ran out of turns, a step was denied, the PR is your own (GitHub does not allow approving it), or the review ran without its agents. Look at the posted `Not verified` section, then at the kept log in `drafts/<repo>-<pr>-<sha>.jsonl` (`dexctl` and the DM list denials). Cost scales with the PR: a small PR should be well under $2, a huge one can reach $8; cap a single review with `bin/dexctl cap job-usd review N`.

## Policy: who may do what, and limits
With no `policy.json`, **everyone may do everything, unlimited**; usage is still recorded. Copy `policy.example.json` to `policy.json` to add roles and limits (per person, per capability, per day, optional dollar budgets); edits apply without a restart. `@bot usage` shows your usage; the owner sees everyone's. Every decision and action is appended to `audit.jsonl`.

## Safety
- Reviews run in a throwaway worktree with a private temp dir; the only write access is that temp dir. `gh pr merge`, `git push`, `git commit` and any `../` path are denied. A review never runs the PR's own code (no `pytest`, `ruff`): test results come from CI.
- The review allowlist (`dex/tools.py`) is strict on purpose: the skills' scripts are allowed as `.../scripts/*` (the `:*` form never matched), a chained command is allowed only if every part is allowed, shell variables are denied, and the review is posted with `--body-file` (an inline multi-line body with a `#` line is denied). Denied calls are counted in the review's usage line and all logged to `denials.jsonl` (to adjust the allowlist in one batch); only the risky ones (anything that writes, publishes, runs PR code or reaches out) DM the owner. A run may read and list its own private temp dir (`--add-dir`), where the fetch dir lives; sibling repos and `..` stay out of reach on purpose. Raw event logs of reviews that were denied something, failed, or were comment-only are kept in `drafts/` to see why.
- Review prompts and skills are untrusted-input aware: PR text is data, never instructions. Full permission bypass is not used.
- The assistant is read-only: it reads only the repo checkouts (not the bot's `.env`), runs git only as `git -C <checkout> <read-only subcommand>`, and uses GitHub only for PR and search lookups. Edit, push, commit and merge are denied.
- Slack messages, PR text, logs and code are treated as untrusted data by every prompt.

## Tests
```
.venv/bin/python tests/test_policy.py && .venv/bin/python tests/test_claude_env.py && .venv/bin/python tests/test_core.py && .venv/bin/python tests/test_flow.py && .venv/bin/python tests/test_tools_cli.py && .venv/bin/python tests/test_botq_code.py && .venv/bin/python tests/test_ctl.py
.venv/bin/python tests/eval_router.py     # real model calls (~$0.05)
tests/check_permissions.py   # real model calls (~$0.9): every skill script and read command is allowed, merge/push/rm/inline-body are not.
                             # Run it after editing dex/tools.py or updating a review skill.
```
