# Configuration

Every identity-bearing file is local and ignored by git, so a fork never inherits anyone else's Slack app, Slack id, GitHub login or repos.

## Files
| File | Purpose | Example to copy |
|---|---|---|
| `.env` | Slack tokens, owner, org, repos, timeouts (secret) | `env.example` |
| `people.json` | Slack id → name and GitHub login (so "what is Ada doing?" works); edit freely | `people.example.json` |
| `repos_extra.json` | More reviewable repos, same shape as `REPOS`, so `.env` stays untouched | `repos_extra.example.json` |
| `readonly_repos.json` | Repos the assistant may read and search but never review | `readonly_repos.example.json` |
| `policy.json` | Roles, per-person limits, switches (without it: everyone may do everything, unlimited) | `policy.example.json` |
| `claude_profiles.json` | Optional: redefine or add Claude accounts | `claude_profiles.example.json` |

`make setup` copies `env.example` and `people.example.json` for you (only when the real file is missing).

## Settings in `.env`
| Setting | Meaning | Default |
|---|---|---|
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | The two Slack tokens (`xoxb-…`, `xapp-…`): see [slack-app.md](slack-app.md) | required |
| `OWNER_USER_ID` | **Your** Slack member id. The owner gets the drafts and the alerts and can do everything | required |
| `GITHUB_ORG` | Your org; "is anyone working on X" searches run over it | empty = that search is off |
| `REPOS` | `{"owner/name": {"clone": "/path/to/clone", "skill": "/review-prs"}}`: only the repos you work on | empty = questions only |
| `SKILLS_REPO`, `SKILLS_REF`, `SKILLS_AUTO_INSTALL` | Where the review skills come from: see [skills-and-repos.md](skills-and-repos.md) | placeholder, `main`, `1` |
| `BOT_NAME` | How the assistant names itself in its prompts (the Slack name is set in the Slack app) | `Dex` |
| `ALLOWED_USERS` | Comma-separated Slack ids allowed to talk to the bot | empty = anyone |
| `JOB_TIMEOUT_MIN` / `QA_TIMEOUT_MIN` | One review / one answer | `45` / `10` |
| `CONCURRENCY` / `QA_CONCURRENCY` | Reviews / answers at a time (two reviews in parallel also draw down the account's usage side by side) | `1` / `2` |
| `MAX_QA_TURNS_PER_THREAD`, `QA_IDLE_HOURS` | Turns per thread; idle hours before a thread session expires | `15`, `24` |
| `QA_BARE` | `--bare` for answers (about 4.6x cheaper) | `1` |
| `QA_REFRESH_SECONDS` | How often `origin/main` is fetched in the existing clones | `600` |

## Policy: who may do what
With no `policy.json`, **everyone may do everything, unlimited**; usage is still recorded. Copy `policy.example.json` to `policy.json` to add roles and
limits (per person, per capability, per day, optional dollar budgets); edits apply without a restart. `@bot usage` shows your usage; the owner sees everyone's.
Every decision and action is appended to `audit.jsonl`. Switches and caps are also available as commands: see [operations.md](operations.md).
