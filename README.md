# Dex

**A Slack bot that reviews pull requests and answers questions about your code.** It runs on your own machine (Slack Socket Mode, no public URL), with your
own Slack app, GitHub login and Claude account, and it never merges or pushes anything.

| You say in Slack | Dex does |
|---|---|
| a PR link (or "review" in a thread under one) | **Reviews the PR** with a review skill, which posts its own decision to GitHub. Several links in one message are queued and reviewed one at a time. |
| any other question ("who changed this and why?", "my open PRs") | **Answers** from read-only access to your repos' `main`, git history and GitHub. A thread is one session, so follow-ups keep context. |
| status / stop / usage | Shows what is running, **stops** a job, shows **usage**. |

No command format is needed: a small LLM router decides what a message is. Details: [docs/using-dex.md](docs/using-dex.md).

## Quick start
1. **`make setup`**: venv, dependencies, config files from the examples, and the review skills your repos need.
2. **Create the Slack app** from [`slack-app-manifest.yml`](slack-app-manifest.yml) and get its two tokens: [docs/slack-app.md](docs/slack-app.md).
3. **Fill in `.env`**: the Slack tokens, **your** `OWNER_USER_ID`, your `GITHUB_ORG`, and **only the repos you work on** in `REPOS`: [docs/configuration.md](docs/configuration.md).
4. **Log in**: `gh auth login`, and a Claude account (company, or a personal subscription): [docs/claude-accounts.md](docs/claude-accounts.md).
5. **`make doctor`** shows what is still missing, then **`make run`**.

Nothing about the author's setup ships in the repo: every identity file (`.env`, `people.json`, `policy.json`, `repos_extra.json`,
`claude_profiles.json`) is local and git-ignored, so each fork has its own Slack app, owner, repos and accounts.
`make help` lists all targets (`setup`, `skills`, `skills-update`, `doctor`, `run`, `test`).

## Documentation
| Page | What is in it |
|---|---|
| [Configuration](docs/configuration.md) | Every file and `.env` setting, roles and limits (`policy.json`) |
| [Slack app](docs/slack-app.md) | Create the app from the manifest, the two tokens, the scopes |
| [Repos and review skills](docs/skills-and-repos.md) | Which repos and skills, automatic skill install, the .NET monolith flow |
| [Claude accounts](docs/claude-accounts.md) | Company account vs personal subscription vs API key, and why; switching |
| [Using Dex](docs/using-dex.md) | The review card, queue, big PRs, usage limits, what the assistant can see |
| [Operations](docs/operations.md) | `dexctl`, spend caps, the Dex page, running and restarting |
| [Safety](docs/safety.md) | What reviews and the assistant may and may not do |
| [Development](docs/development.md) | Tests, code layout, contributing ([CONTRIBUTING.md](CONTRIBUTING.md)) |
| [Two accounts research](docs/claude-two-accounts.md) | Background notes on running two Claude accounts |
| [Open-source plan](docs/open-source-plan.md) | How Dex was made forkable |

> **Do not restart Dex while a review runs: it kills it.** Details in [docs/operations.md](docs/operations.md#running-it).
