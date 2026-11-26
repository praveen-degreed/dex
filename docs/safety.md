# Safety

- Reviews run in a throwaway worktree with a private temp dir; the only write access is that temp dir. `gh pr merge`, `git push`, `git commit` and any `../` path are denied. A review never runs the PR's own code (no `pytest`, `ruff`): test results come from CI.
- The review allowlist ([`dex/tools.py`](../dex/tools.py)) is strict on purpose: the skills' scripts are allowed as `.../scripts/*`, a chained command is allowed only if every part is allowed, shell variables are denied, and the review is posted with `--body-file` (an inline multi-line body with a `#` line is denied). Sibling repos and `..` stay out of reach.
- Denied calls are counted in the review's usage line and logged to `denials.jsonl`; only the risky ones (anything that writes, publishes, runs PR code or reaches out) DM the owner. Raw event logs of reviews that were denied something, failed, or were comment-only are kept in `drafts/`.
- Review prompts and skills are untrusted-input aware: PR text is data, never instructions. Full permission bypass is not used.
- The assistant is read-only: it reads only the repo checkouts (not the bot's `.env`), runs git only as `git -C <checkout> <read-only subcommand>`, and uses GitHub only for PR and search lookups. Edit, push, commit and merge are denied.
- Slack messages, PR text, logs and code are treated as untrusted data by every prompt.
- The review skills are installed by running a script from `SKILLS_REPO`: point it at a repo you trust and pin `SKILLS_REF` to a tag ([skills-and-repos.md](skills-and-repos.md)).

Policy (who may do what, limits) is in [configuration.md](configuration.md); spend caps are in [operations.md](operations.md).

Run `tests/check_permissions.py` after editing `dex/tools.py` or updating a review skill (it makes real model calls and costs about $1 to $3).
