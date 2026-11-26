# Using Dex: reviews and questions

## What a finished review looks like
The thread reply is a small card; the icon follows the result (approved, changes requested, comments posted):

```
:speech_balloon: @requester Your review of web-app #4482 is done.
*Result*  Comments posted  ·  View the review

> :bar_chart: *Usage*
> *Tokens*  `11.20M`  (920K new · 10.26M cached · 17K output)
> *Cost*  `$8.17`  _API-equivalent estimate, not billed per token_
> *Agents*  `19`      *Time*  `14m 13s`
> *Account*  Personal Claude plan (subscription)
> :no_entry_sign: 1 tool call blocked by safety rules

:speech_balloon: _Addressed the comments? Ask me to review again right here in this thread ..._
```
- The reply **tags the person who asked** (`@name`), so they are notified when it is done, failed, or stopped at the cost limit.
- The last line only appears when there are comments to address, so the re-review stays in the same thread. The blocked-calls line only appears when something was blocked.
- On a subscription the cost is labelled an API-equivalent estimate; on Foundry it is just the cost. The account is read when the review starts, so a switch during a run does not change what is reported. The audit log records the same `profile`, `agents` and `tokens`.

## Queue
Reviews run `CONCURRENCY` at a time. A review that has to wait is announced in its thread, tagging the requester: queued, #N in line, behind which
review (and how many more). The moment it is picked up Dex posts "Your turn: the review of ... is running now (it waited 12m 04s)". A review that can start
at once just says "Review started". Questions and finished reviews do not count as ahead.

## Big PRs: Dex asks first
A PR above 50 files or 3000 changed lines is not run straight away. Dex asks the requester in the thread whether it can be split
(a smaller PR gets a better review and uses far fewer tokens).
- If it can, the requester sends the smaller PRs. "I'll split it" or "not now" drops the request.
- If it can't, the requester says so in the thread (no mention of Dex needed; an LLM reads the answer, not a keyword) and Dex reviews it **fully**: the skill is told the PR cannot be split, so it reviews every unit instead of stopping at its unit limit.
- Only the requester or the owner can answer; an unanswered question is forgotten after a day; the owner's `force` skips the question.
- Limits: `bin/dexctl cap big-pr <files> [lines]`, `off` or `default` (`dexctl show` prints them). A fully reviewed huge PR is expensive; `dexctl cap job-usd review N` still caps one review.

## When a review is not approved
The skill approves only a complete review. Common reasons for a comment-only review: a reviewer ran out of turns, a step was denied, the PR is your own
(GitHub does not allow approving it), or the review ran without its agents. Look at the posted `Not verified` section, then at the kept log in
`drafts/<repo>-<pr>-<sha>.jsonl` (`dexctl` and the owner's DM list denials). Cost scales with the PR: a small PR should be well under $2, a huge one can reach $8.

## When the Claude account reaches its usage limit
If a run fails because the account behind it (Foundry or the personal plan) hit its usage or rate limit, Dex says so instead of a generic "failed":
- the thread reply tags the requester ("the personal Claude plan has reached its usage limit, it resets around HH:MM, send it again after the reset");
- the owner gets a DM with what Claude actually said and which `dexctl` command switches account;
- the review can be retried on the same commit.

Dex then remembers the limit and **does not start new reviews on that account** until it resets (the message's reset time, or 30 minutes if it gave none); the owner can
still force one. Every raw limit message is appended to `limits.jsonl`. The wording of a real limit message is not documented, so detection is deliberately broad
(usage limit, rate limit, 429, quota, "resets at ..."); it only looks at runs that already failed, and `limits.jsonl` shows what Claude really said so the patterns can be tightened.

## What the assistant can see
Read-only checkouts of `main` for every repo in `REPOS` and `readonly_repos.json`, built as `git worktree`s of your clones under `qa/` (they share git history;
your working copies, branches and uncommitted work are never read). A background thread creates and refreshes them every 10 minutes, all repos in parallel, so a
huge first checkout never blocks an answer; a repo that is not ready yet is listed to the assistant as such. GitHub questions ("is anyone working on X") search the
whole `GITHUB_ORG` with `bin/botq find`, and every answer states where it looked.
