# Plan: make Dex forkable and open internally

Status (2026-11-26): phases 1 to 6 implemented on branch `queue-all-prs`, uncommitted; `make test` passes (9 files).
Done since: `LICENSE` (MIT, Degreed, Inc.), `TODO.md` removed from the repo (kept locally, git-ignored).
Not done, waiting on a decision: `dexctl init`, the `review-skills` public-repo scan, branch protection.

## Goal
Anyone at the company can fork Dex, run `make setup`, and get their own bot with their own identity, their own repos and the
review skills installed for them. Nothing of the original author's setup ships in the repo.

## Decisions already made
- **Identity is per fork.** Each person creates their own Slack app (their own tokens), sets their own `OWNER_USER_ID`, and reviews
  are posted by whoever is logged in with `gh`. All of this lives in gitignored files (`.env`, `people.json`, `policy.json`,
  `claude_profiles.json`), so a fork never inherits the author's identity.
- **Repos are per fork.** Not everyone reviews the same repos. Each person lists only theirs (`REPOS` in `.env` or `repos_extra.json`).
- **The review skills live in a public repo (`review-skills`).** Dex installs them automatically when they are missing; setup does it
  on a fresh machine. A public repo means a plain `git clone` works: no `gh` access needed to fetch them.
- **A Makefile is the entry point** (`make setup`, `make skills`, `make run`, `make test`). It only wraps code that `dexctl` and Dex
  startup also use, so there is one implementation.

## Phase 1: clean-up (small, safe)
| Item | Change |
|---|---|
| Dead code | Remove `config.repo_key()` and `Policy.remaining()` (defined, never used). |
| `.gitignore` | Add `control/` (runtime inbox/results written by `dex/control.py`; it shows up as untracked today). |
| README | Drop the `legacy/` line (gitignored, absent for forks); document that `eval_router.py` and `check_permissions.py` call the real model and cost money (about $0.05 and $1 to $3). |
| `TODO.md` | It is the author's work log (teammate PR numbers, personal decisions, "needs restart + push"). Cut it to a short roadmap, or move the items to issues. Ask before deleting anything. |
| `docs/claude-two-accounts.md` | Re-read it for a reader who does not have the author's two accounts. |
| Local only (not shipped) | `.env.example` is an old, ignored duplicate of `env.example`; `drafts/` is 169 MB of old reviews; logs and `legacy/` stay local. Delete by hand when ready. |

## Phase 2: remove the author's setup from tracked files
- **Neutral examples.** `env.example` and `repos_extra.example.json`: replace `/Users/...` paths and `degreed/...` names with
  `/path/to/clone` and `your-org/your-repo`. One placeholder repo, plus comments showing the three shapes (FE, Python BE,
  .NET with `dex_posts`).
- **Tests.** Replace real PR numbers, real repo names and teammate names (`sudha`, `praveen`) in `tests/test_flow.py`,
  `tests/test_tools_cli.py` and `tests/_env.py` with fakes (`acme/web`, `ada`, `alan`).
- **`GITHUB_ORG`.** Today it defaults to `"degreed"` (`dex/config.py:30`). Make it explicit: warn at startup if it is not set.
- **README.** Line 55 points at `praveen-degreed/review-skills`; replace it with the `SKILLS_REPO` setting. Rewrite the repo section as
  "pick the ones you need" with this table:

| Repo type | Skill | Extra setting |
|---|---|---|
| Frontend | `/review-prs` | none |
| Python backend | `/review-prs-be` | none |
| .NET monolith | `/review-prs-net {url}` | `"dex_posts": true` |

## Phase 3: repos as pure configuration
- `REPOS` becomes optional (default `{}`). With none configured, Dex starts, logs "no reviewable repos configured" and still
  answers questions on read-only repos.
- Validate at startup: each `clone` path is a git repo and each `skill` exists. A bad entry disables that repo with a clear log line;
  it never crashes the bot or fails mid-review.
- `tools.py` builds the skill-script allowlist from the configured skill names instead of the fixed three.
- `tests/check_permissions.py` only exercises configured repos and skills.

## Phase 4: automatic skill install
New module `dex/skills.py`, used by startup, `dexctl` and the Makefile.

Settings (all optional, in `.env`):
- `SKILLS_REPO`: public repo with the skills (default: the current location until the repo moves).
- `SKILLS_REF`: branch or tag (default `main`; pin a tag for reproducible installs).
- `SKILLS_AUTO_INSTALL`: default `1`. Install only what is missing; never overwrite on its own.

Behaviour:
1. Work out which skills are needed from the configured repos (`/review-prs` = `fe`, `/review-prs-be` = `be`, `/review-prs-net` = `net`).
2. For every Claude skills folder (`~/.claude`, and each `config_dir` in `claude_profiles.json` that exists), check for
   `<skill>/SKILL.md`.
3. If missing: `git clone --depth 1 --branch <ref>` the public repo into `<data>/skills-src`, then run its `install.sh <kind>` with
   `REVIEW_SKILLS_SRC=<clone>` and `CLAUDE_HOME=<folder>`. This needs no change to `install.sh` and no `gh` login. The installer
   already backs up a previous install.
4. If the install fails (offline, wrong repo): log the reason and disable only the repos that need that skill.
5. Updates are explicit: `dexctl skills update` (or `make skills-update`). Dex never changes its reviewer silently while running.

Commands: `dexctl skills check | install | update`.

Safety note for the README: this runs a script from `SKILLS_REPO` on the user's machine, so it must be a repo the company controls;
pin `SKILLS_REF` to a tag for review.

## Phase 5: Makefile
```
make help           list targets
make setup          venv + dependencies + copy *.example files that are missing + install skills + print what is left to fill in
make skills         install missing review skills        (dexctl skills install)
make skills-update  reinstall from SKILLS_REF            (dexctl skills update)
make run            python -u bot.py
make test           unit tests (no network, no cost)
make doctor         gh auth status, claude auth status, skills check, configured repos check
```
`make setup` must work before `.env` exists, so `dex/skills.py` runs standalone (it reads `.env` itself and tolerates an empty `REPOS`).
It prints a checklist of what only the person can do: the Slack app and tokens, their Slack ID, `gh auth login`, `claude` login.

## Phase 6: docs for forkers
Add a "Fork setup" section to the README (one screen):
1. `make setup`
2. Fill in `.env`: Slack tokens, your Slack ID (`OWNER_USER_ID`), your org (`GITHUB_ORG`), your repos.
3. Optional: `people.json` (Slack id to name and GitHub login), `policy.json` (who may do what), `claude_profiles.json`.
4. `make doctor`, then `make run`.

Also add `LICENSE`, a short `CONTRIBUTING.md` (how to run the tests, no secrets in commits), and a CI workflow that runs
`make test`.

## Before the skills repo goes public (blocking)
Public means readable by anyone on the internet, not only the company. Before flipping `review-skills`:
- Scan the skills, agents, evals and `REVIEW.md.example` for internal names, ticket keys, private repo names (for example
  `degreed-api-version-2`), URLs and real PR numbers.
- The `net` skill depends on skills that live in the private monolith repo (`dg-pr-review`, `dg-maestro-review`). Confirm what it
  reveals about them, or keep `net` out of the public repo.
- Check the git history of `review-skills`, not just the current files.
- Decide whether "public" should really mean internal-only (an internal org repo that every employee can read). The auto-install
  works the same way, but then forks need `gh` access to it.

## Verification
- Unit tests pass (`make test`) after each phase.
- A fresh-clone dry run in an empty directory with an empty `CLAUDE_HOME`: `make setup` completes, `make doctor` reports exactly the
  missing manual steps, skills are installed, `dexctl skills check` is clean.
- A clone with only one repo configured ignores PR links for other repos and installs only that repo's skill.
- A deliberately broken `SKILLS_REPO` disables the affected repos and the bot still starts.

## Open questions
1. Public repo or internal-only org repo for `review-skills` (see the blocking item)?
2. What should `SKILLS_REPO` default to before the repo moves: a placeholder that forces setting it, or the current location?
3. Should `TODO.md` be cut down in place, or moved to GitHub issues?
4. Is a `dexctl init` (interactive: Slack ID, GitHub login, org, repos) wanted, or are `make setup` plus the README enough?

## Suggested order
Phases 1 and 2 first (cheap, shippable on their own), then 3, then 4 and 5 together, then 6. The skills repo scan is independent and
should start now because it gates going public.
