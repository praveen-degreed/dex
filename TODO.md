# TODO and requests log

The running list of what is open for Dex and the review skills. Newest first. Status: open, decision, in progress, done.

## Requested 2026-11-17
- **done (needs restart + push)** A review no agent worked on, or a `DEX_VERDICT: skip`, posts nothing to GitHub (only a Slack reply to the requester); drafts are refused for .NET repos. Caused by the first .NET run on a draft PR (#58217): the skill stopped at preflight and Dex still posted its 'review not run' text as a comment on that PR. That comment is still on the PR and needs to be hidden or deleted by hand.
- **done (needs restart + push)** Tag the person who requested a review, in the thread reply, when it finishes, fails or stops at the cost limit.
- **in progress** Post a real decision for every review: the .NET flow was comment-only; now the skill ends with `DEX_VERDICT: approve|request_changes|comment` and Dex posts `--approve` / `--request-changes` / `--comment` (approve downgraded when no agent ran; own PR falls back to a comment). Dex side done; the skill side is `review-skills` PR #5, waiting for merge. Note: an approval posted under the owner's login counts like a human approval for checks such as bot-approval-guard.
- **done (needs restart + push)** Post tokens and cost at the end of every review. A line in the Slack thread reply: total tokens (fresh, cached, output), cost, number of agents, duration and the account used. On the personal subscription the cost is an API-equivalent estimate. Needs the account recorded at the start of the review (see the audit bug below). Also decide whether to add it to the GitHub comment (default: Slack only).
- **open (later)** Run reviews in parallel. Today `CONCURRENCY=1`, so a second review waits. Needs `CONCURRENCY` raised and a Dex restart; test two small reviews first (same-clone worktrees, memory, rate limits on a subscription).
- **decision** The `review` account switch applies to everyone's reviews, so teammates' reviews also run on the personal account. Decide: keep, or personal only for reviews the owner requests.
- **done** The audit records the Claude profile read at the start of the review (it was read at the end).
- **in progress** First real run of `review-prs-net` (the .NET monolith skill) on the personal account. Check: route, team skill followed, agents launched, citation check, tokens and cost, what was posted.
- **decision** Personal and company account design. Scope for now: Dex reviews only. Keep two folders (current) or one shared folder (see `docs/claude-two-accounts.md`). The terminal switch `USE_PERSONAL_CLAUDE=0|1` exists and works with two folders.
- **open** Enterprise startup tokens are higher (about 35K vs 25K per session) because the normal folder loads 12 skills and 9 plugins. Trim the ones rarely used.

## Open from earlier
- **decision** Old repos `fe-claude-skills` and `be-claude-skills` are archived but still public; decide private or delete. `dev-guard` lives in the frozen `fe-claude-skills` and needs a new home.
- **open** Measure the `py-docs-conformance` cap (60 to 40 turns) on the next large BE review; check it did not cut depth.
- **open** A fair baseline comparison against the built-in `/code-review` (it reviewed the wrong diff in three runs because of the clone's stale local `main`).
- **open** Slack token refresh support (needed before any reinstall of the Slack app) and rotate the tokens that were pasted in chat.
- **open** A GitHub token that cannot merge to `main`, so "no merge" is enforced server-side.

## Done 2026-11-17
- One repo for both review skills (`review-skills`), plus `review-prs-net` for the .NET monolith.
- Account switch for Dex runs (`dexctl foundry|subscription`, `dexctl claude show|check|use`), terminal switch `USE_PERSONAL_CLAUDE`.
- Dex warns when a review ran no agents; reviews post with `--body-file`; stale worktrees are cleared.
