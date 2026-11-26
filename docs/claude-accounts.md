# Which Claude account Dex runs on

Every review, answer and routing decision is a `claude` run, and that run needs an account. Dex can send each kind of run (`review`, `question`, `router`)
to a different account and switch **without a restart**.

**Why this exists.** In our setup the work account is Azure Foundry: an API-style account, billed per use, with no login (it is selected by environment
variables). A big review can cost real money (one unposted review cost about $25). A Claude subscription has a flat price but usage limits, while Foundry has none.
So reviews can be sent to a personal subscription, while questions and the router stay on the company account.

> **Reviewing private company code on a personal account may break company policy. Check before you use one.**

| | **Enterprise / company account** (default) | **Personal Claude subscription** | **Personal API key** |
|---|---|---|---|
| What it is | The environment Dex is started with: Azure Foundry, Bedrock, Vertex or a company API key | A separate claude.ai (Pro/Max) login in its own folder | A key taken from your environment |
| Profile name | `foundry` (historical: it means "the environment as started") | `subscription` | your own, in `claude_profiles.json` |
| Billing / limits | Per use; no subscription limits | Flat price; usage limits (Dex stops starting reviews when one is hit) | Per use |
| Setup | none | Steps B | Steps C |

## A. Enterprise account only (the default)
Nothing to switch. Export your provider variables in the shell that starts Dex (for Azure Foundry: `CLAUDE_CODE_USE_FOUNDRY=1`,
`ANTHROPIC_FOUNDRY_RESOURCE=<resource>`, the `ANTHROPIC_DEFAULT_*_MODEL` names of your deployments, and `az login`). Check with `claude auth status`
(it should name your provider), then `make doctor`. Bedrock, Vertex and a company API key work the same way: whatever environment Dex starts with is the `foundry` profile.

## B. Add a personal subscription for reviews
Dex never logs in for you.
1. Make its own folder and log in there: `mkdir -p ~/.claude-subscription`, then `CLAUDE_CONFIG_DIR=~/.claude-subscription claude` and `/login` with the personal account. (That folder is the default; change it in `claude_profiles.json`.)
2. Install the review skills into that folder too: `bin/dexctl skills install` (or `make skills`). Dex also does it at the next start once the folder exists.
3. `bin/dexctl claude check subscription` must say `loggedIn=True`.
4. Switch: `bin/dexctl subscription` (reviews only). Back: `bin/dexctl foundry`.

## C. A personal API key instead of a login
Define a profile in `claude_profiles.json` that unsets the provider variables and takes the key from your environment (`set_from_env`), so the key is never
written to a file; the `my-api-key` entry in `claude_profiles.example.json` is the template. Then `bin/dexctl claude use review my-api-key`.

## Switching and checking
```
bin/dexctl claude show                       which account each kind of run uses
bin/dexctl claude check subscription         is that account usable (runs `claude auth status` with its environment)
bin/dexctl claude use review subscription    reviews on the subscription; question / router / all work too
bin/dexctl claude use all foundry            everything back on the company account (the default)
bin/dexctl subscription                      shortcut: reviews on the subscription (questions and router unchanged)
bin/dexctl foundry                           shortcut: everything back on the company account
```

## How it works, and why two folders
- The `subscription` profile starts `claude` with its own `CLAUDE_CONFIG_DIR` and **without** the Foundry, cloud-provider and API-key variables, so Claude Code uses its normal login.
- The choice is stored in `policy.json` (`claude_profile`) and read when each run starts. A run set to a profile that is missing, not logged in or incomplete **fails with a clear message and never falls back** to the other account. The audit log records the profile of every review.
- **Why two folders:** a Claude config folder holds the login, settings, plugins, skills and history, and two folders share nothing, so the personal account's data stays apart from the company's. The cost is that skills and plugins must be installed in both, which `dexctl skills` does for you. One shared folder is possible (the login is outranked while the provider variables are set) but relies on behaviour the docs do not state; the research and trade-offs are in [claude-two-accounts.md](claude-two-accounts.md).
- Caveats: `--bare` skips the login, so on the subscription the router and answers run without it (more startup tokens each); a subscription has usage limits that Foundry does not.
