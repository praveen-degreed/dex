# TODO and requests log

The running list of what is open for Dex and the review skills. Cleaned up 2026-11-17 (everything below "Done" is live: Dex restarted 21:21, pushed at `b3ac260`).

## Needs you
- **decision** Teammates' reviews also run on the personal account (the `review` switch is global). Keep, or personal only for reviews the owner requests?
- **decision** Personal vs company account design. Scope for now: Dex reviews only; two folders (current) work. One shared folder is the alternative (see `docs/claude-two-accounts.md`).
- **decision** Old repos `fe-claude-skills` and `be-claude-skills` are archived but still public: make private or delete. `dev-guard` lives in the frozen `fe-claude-skills` and needs a new home.
- **action** `degreed/Degreed` #58217: rerun the review (drafts are reviewed now). Its earlier junk comment ("Review not run ... I posted nothing to GitHub") is still on the PR and has to be hidden by hand (or ask me to try minimizing it).

## Open work
- **open** Test the Dex page controls (stop, run, restart, pause, switches, caps, Claude accounts) on a throwaway state; only the page and the read-only API were checked.
- **open** Run the full live permission check once, `tests/check_permissions.py` (~$0.95), after today's allowlist changes.
- **open (later)** Reviews in parallel. `CONCURRENCY=1` today; needs a raise in `.env` plus a restart; test two small reviews first.
- **open** Trim the enterprise startup load (about 35K vs 25K tokens per session): 12 skills and 9 plugins in the normal folder.
- **open** Measure the `py-docs-conformance` cap (60 to 40 turns) on the next large BE review; check it did not cut depth.
- **open (optional)** Fair baseline vs the built-in `/code-review` (it reviewed the wrong diff in three runs: the clone's local `main` is stale).
- **open** Slack token refresh support (needed before any reinstall of the Slack app) and rotate the tokens that were pasted in chat.
- **open** A GitHub token that cannot merge to `main`, so "no merge" is enforced server-side.

## To confirm on a real run
- `review-prs-net` posting a real decision (`DEX_VERDICT: approve|request_changes|comment`). It has run twice so far: #58450 (comments, 6 agents, 8.4M tokens) and #58217 (stopped at the draft check, before the fix). The approve / request-changes path has not been seen yet.
- The usage line, requester tag and "N tool calls blocked" count in a real thread reply (shipped after the last review finished).

## Done 2026-11-17
- One repo for the review skills (`review-skills`: FE, BE, and `review-prs-net` for the .NET monolith, which routes to `dg-pr-review` / `dg-maestro-review`); installer copies the shared scripts; both Claude folders kept in step.
- Skill rules: mandatory agents gate, size/risk selection, shared digest, free scripts, verify-citations, always-run mapped reviewers, no ping agents; `.NET` skill decides approve / request changes / comment and ends with a `DEX_VERDICT` line.
- Dex: account switch per kind of run (`dexctl foundry|subscription`, `dexctl claude show|check|use`; the account is read at the start of a review); terminal switch `USE_PERSONAL_CLAUDE=0|1`; monolith and degreed-assistant reviewable (`repos_extra.json`); several PR links in one message; reviews post with `--body-file`; stale worktrees cleared.
- Every finished review: usage line (tokens, cost, agents, time, account), tags the requester, warns when no agent ran; a review no agent worked on (or `skip`) posts nothing to GitHub; drafts are reviewed.
- Recurring denials fixed at the root: the run's own temp dir is readable, read-only `grep` / `rg` / `jira issue view` / `git -C <dir> <read-only>` allowed, all denials logged to `denials.jsonl`, only risky ones DM the owner. Blocked on purpose: `..`, sibling repos, `pytest`, web fetch/search, inline-body `gh pr review`, `git -C <dir> push|commit|checkout|reset`.
- The Dex page (`bin/dexctl serve`): overview, jobs, settings and controls, guarded by Host + Origin + a per-process token. Later: live findings per agent, a live cost estimate, queue reordering.
