# Operations: spend, controls and the Dex page

## `dexctl`
```
bin/dexctl status                  what is running now (live steps and tokens), the switches and caps
bin/dexctl usage [--days N]        spend and tokens: today per person, and the last N days
bin/dexctl top [--n N]             the most expensive actions
bin/dexctl pause | resume          stop / allow all costly work (status, stop, usage still answer)
bin/dexctl disable review          switch a capability off for everyone (enable to undo)
bin/dexctl cap job-usd review 4    stop any single review that spends more than $4 (soft cap)
bin/dexctl cap daily-usd 10        the whole bot's spend per day
bin/dexctl cap pr-files 80         refuse reviews of PRs with more than 80 changed files (the owner can force)
bin/dexctl skills [check|install|update]   the review skills the configured repos need
bin/dexctl claude ...              which Claude account each kind of run uses: see claude-accounts.md
bin/dexctl serve [--port 8787]     the Dex page at http://127.0.0.1:8787
```
Everything is unlimited by default. The commands edit `policy.json` (atomically) and Dex picks the change up on its next message, with no restart. In Slack the
owner can say `@Dex pause` / `@Dex resume` / `@Dex what is switched off?`; anyone else is refused. Live token counts come from Claude's stream (fresh input,
cached input, output) and are recorded per person and in `audit.jsonl`.

*Per-job caps use the Claude CLI's `--max-budget-usd`, which is checked between model turns: a job can overshoot a cap by up to one turn (a large review turn is about
$0.05-0.20). A job that hits its cap is stopped, nothing is posted, and the asker and the owner are told.*

## The Dex page
`bin/dexctl serve` opens one page with three views:
- **Overview**: paste a PR link to preview and run a review; spend, reviews, questions and tokens today; live jobs with their steps, current tool call and review agents; recent jobs with Rerun; an activity feed.
- **Jobs**: every job since Dex started, with its tool calls, agents and timeline.
- **Settings**: switch reviews or questions off, pick the Claude account per kind of run, set the limits. Changes are staged and applied together.

Stop, Cancel and Restart wait 5 seconds with an Undo. Restart waits until nothing is running, then restarts Dex in place (same process id, so tmux and caffeinate keep it).
Live cost is not known until a run ends (Claude reports it at the end), so a running job shows tokens and its cap. Reviews started from the page are announced in a Slack
message (to the owner's DM, or `DEX_PAGE_CHANNEL`) and run in that thread like any other.

### How the page is kept safe
The page is its own process, binds to `127.0.0.1` only and rejects any other `Host` header (no DNS rebinding). Reading needs nothing more; every change also needs the
page's own `Origin` and a per-process token sent in a custom header, so another web page cannot make one. Settings go through the same code as `dexctl` (same
validation) into `policy.json`. Stop, run and restart are files dropped in `control/inbox/` that the bot picks up within a second (`dex/control.py`); one older than a
minute is dropped, never acted on late. Every change is in `audit.jsonl` with `"source": "page"`.

## Running it
`tmux new -s dex`, then `caffeinate -i make run`. Code changes load only on a restart (tmux does not reload code or restart a crashed bot).
**Do not restart while a review runs: it kills it** (check `pgrep -fl 'claude.* -p'` first). A killed review leaves nothing posted; send the link again with `force`.
