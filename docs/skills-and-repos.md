# Repos and review skills

## Choosing your repos
Not everyone reviews every repo: list only yours (`REPOS` in `.env`, or `repos_extra.json`). Each reviewable repo names its skill.

| Repo type | Skill | Extra setting |
|---|---|---|
| Frontend | `/review-prs` | none |
| Python backend | `/review-prs-be` | none |
| .NET monolith | `/review-prs-net {url}` | `"dex_posts": true` (in `repos_extra.json`) |

A PR link for a repo that is not listed is ignored. At startup a listed repo whose clone is not a git repository, or whose skill is not installed, is
disabled with a clear log line; the bot still starts. Repos the assistant may only read go in `readonly_repos.json`.

## Automatic skill install
The skills live in a public repo (`SKILLS_REPO`, default `praveen-degreed/review-skills`, at the branch or tag `SKILLS_REF`). Dex installs the ones your repos need **when they are missing**:
at `make setup`, at every start (`SKILLS_AUTO_INSTALL=1`, the default) or on demand with `dexctl skills install`.

- It does a shallow `git clone` (no GitHub login needed) and runs that repo's own `install.sh` for the Claude folder of every account (`~/.claude`, and each `config_dir` in `claude_profiles.json`).
- It only fills in what is missing. `dexctl skills update` (or `make skills-update`) reinstalls from `SKILLS_REF`; the installer keeps one backup per skill. `dexctl skills check` shows what is installed where.
- A skill change takes effect after it is merged in that repo and reinstalled. No Dex restart is needed: each review reads the installed skill fresh.

> **Security:** this runs a script from `SKILLS_REPO` on your machine. Point it at a repo you trust and pin `SKILLS_REF` to a tag.

## The .NET monolith in detail
Dex runs one main skill for it, `/review-prs-net` (from the skills repo), which routes to the team's own skills in the repo:
`/dg-maestro-review` when a changed path matches `maestro|aicoach`, otherwise `/dg-pr-review --analysis-only`. Those skills only draft.

A repo entry (in `repos_extra.json`) with `"dex_posts": true` makes Dex run the skill read-only on a worktree of the PR and then **post the draft itself
with the skill's decision**. The skill ends its review with `DEX_VERDICT: approve|request_changes|comment`, and Dex posts it with `--approve`,
`--request-changes` or `--comment`.

- `skip` means nothing was reviewed. If no review agent ran, or the verdict is `skip`, **nothing is posted to GitHub**: Dex only tells the requester in the thread.
- Draft PRs are reviewed like any other. On the poster's own PR Dex falls back to a comment (GitHub does not allow approving your own).
- A skill string may carry `{number}` / `{url}`. The older `"maestro_skill"` / `"maestro_paths"` keys (routing done by Dex itself) still work for repos without such a skill.
- A PR that changes anything under `.claude/` is refused (the skills come from the PR itself, so it could rewrite its own reviewer); the owner can force it.
- Not benchmarked yet: the first real run of each skill is the test. A large monolith (tens of thousands of files) makes the review worktree slow to create.

See `repos_extra.example.json`.
