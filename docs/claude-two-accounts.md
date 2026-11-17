# Claude Code: company Azure Foundry + personal claude.ai

Note: this repo had no existing notes convention; this file is a one-off research note.
Sources are primary only (code.claude.com docs, local `claude --help`), fetched 2026-11-17. Claude Code 2.1.322.
"(inferred)" marks conclusions the docs do not state outright.

## Summary
1. CLAUDE_CONFIG_DIR relocates everything under ~/.claude and ~/.claude.json (settings, plugins, skills, hooks, memory, MCP, history, credentials); nothing is shared between two dirs.
2. Credentials are per config dir (Keychain entry keyed to the dir on macOS, `.credentials.json` as fallback); provider (Foundry) auth comes from env vars, not from the login.
3. Foundry env vars outrank a stored claude.ai login, so ONE dir can hold both: Foundry vars set = Foundry, unset = claude.ai (inferred from the documented precedence).
4. Provider vars can live in settings.json `env`, and a settings `env` value overrides the same variable exported in the shell.
5. Recommendation: one shared ~/.claude, log in to claude.ai there once, and enable Foundry only per launch via a wrapper; use a second dir only if you need isolation.

## 1. What CLAUDE_CONFIG_DIR changes
- Overrides the config dir (default `~/.claude`); "All settings, session history, and plugins are stored under this path" and credentials too. Settable in shell, user settings or managed settings; in a settings file use an absolute path; ignored in project/local settings. https://code.claude.com/docs/en/env-vars (variables table, `CLAUDE_CONFIG_DIR`)
- Every `~/.claude` path lives under it, "The same applies to `.claude.json`, which otherwise sits at `~/.claude.json`." https://code.claude.com/docs/en/claude-directory (intro)
- What lives there: `settings.json` (permissions, hooks, env), `CLAUDE.md`, `skills/`, `agents/`, `commands/`, `rules/`, `plugins/` (installed plugins), `projects/<project>/memory/` (auto memory), `history.jsonl`; `.claude.json` holds account details, personal MCP servers, per-project trust. https://code.claude.com/docs/en/claude-directory (file reference, "What's not shown")
- Therefore two dirs share NOTHING: no settings, plugins, skills, hooks, MCP servers, memory or session history. Each must be configured separately (inferred from the above; the docs say "Each directory has its own settings, session history, and claude.ai login or API key", https://code.claude.com/docs/en/authentication, "Log in with multiple accounts").
- Not relocated: project-level files (`.claude/`, `CLAUDE.md`, `.mcp.json` in the repo) are read from the project either way (inferred; they are project scope, https://code.claude.com/docs/en/claude-directory).
- Exception: Anthropic profile credentials live in `~/.config/anthropic`, outside the config dir; Console keyless sign-ins are stored outside it too. https://code.claude.com/docs/en/authentication ("Anthropic profiles...", "Log in with multiple accounts")

## 2. Where credentials live; can one dir hold both?
- macOS: encrypted Keychain; if the Keychain rejects the write, `~/.claude/.credentials.json` (mode 0600). Linux/Windows: `.credentials.json`. With CLAUDE_CONFIG_DIR set, the file is under that dir and "keys the macOS Keychain entry to that directory too, so a session with a different `CLAUDE_CONFIG_DIR` reads a different entry." https://code.claude.com/docs/en/authentication ("Credential management")
- Foundry credentials are not a login: API key (`ANTHROPIC_FOUNDRY_API_KEY`), Entra ID via the Azure default credential chain (e.g. `az login`), or bearer token (`ANTHROPIC_FOUNDRY_AUTH_TOKEN`). `/logout` is unavailable under Foundry. https://code.claude.com/docs/en/microsoft-foundry (Setup step 2)
- Precedence when several credentials exist: 1) cloud provider when `CLAUDE_CODE_USE_BEDROCK/VERTEX/FOUNDRY` is set; 2) `ANTHROPIC_AUTH_TOKEN`; 3) `ANTHROPIC_API_KEY`; 4) `apiKeyHelper`; 5) `CLAUDE_CODE_OAUTH_TOKEN`; 6) profiles; 7) subscription OAuth from `/login`. https://code.claude.com/docs/en/authentication ("Authentication precedence")
- Answer: YES, one dir can hold a claude.ai login AND be used for Foundry, selected only by env vars (inferred): the login sits at rank 7 and is simply outranked while `CLAUDE_CODE_USE_FOUNDRY` is set. The docs never state this combination explicitly, and no `claude auth` subcommand switches provider (`claude auth --help`: login, logout, status).
- Verify each mode with `claude auth status` / `/status`: Foundry shows "Microsoft Foundry" as API provider. https://code.claude.com/docs/en/microsoft-foundry (Setup step 5)

## 3. Settings sources and env
- Four sources: User `~/.claude/settings.json`; shared project `.claude/settings.json`; project local `.claude/settings.local.json`; managed (`managed-settings.json`, MDM, server-managed). https://code.claude.com/docs/en/settings ("Settings files and who they affect")
- Precedence, highest first: managed, command line (`--settings <file-or-json>`), project local, shared project, user. https://code.claude.com/docs/en/settings ("Settings precedence")
- Env vars are not a level; the `env` key is "an ordinary key and follows the levels above". https://code.claude.com/docs/en/settings ("Settings precedence")
- Provider vars in `env`: allowed. "Add variables under the `env` key in a `settings.json` file... they take effect no matter how `claude` was launched." https://code.claude.com/docs/en/env-vars ("Set variables in settings files"). The Foundry page shows only `export` examples and says env vars are "the only configuration path" (no wizard), but does not forbid `env` (https://code.claude.com/docs/en/microsoft-foundry, step 5). `CLAUDE_CODE_USE_FOUNDRY` and `ANTHROPIC_FOUNDRY_RESOURCE` are listed as ordinary variables, so settings `env` should work (inferred).
- Shell vs settings: "When the same variable is set in both your shell and a settings file `env` block, the settings file value applies in most sessions" (it replaces the inherited shell value). https://code.claude.com/docs/en/env-vars ("Precedence"). Exceptions are in the `env` entry of https://code.claude.com/docs/en/settings-reference (not read in full).
- You cannot unset via settings, but can blank: `"CLAUDE_CODE_USE_VERTEX": ""` is "treated as unset for provider selection". https://code.claude.com/docs/en/env-vars ("Precedence"). Same idea should apply to `CLAUDE_CODE_USE_FOUNDRY` (inferred).
- Between settings files `env` follows normal precedence (managed beats user/project); project/local cannot set CLAUDE_CONFIG_DIR or OTel exporter vars. Same section.
- Per-session settings without touching files: `claude --settings '<json or path>'`, applied above user/project/local, below managed. https://code.claude.com/docs/en/settings ("Change a setting for one session")
- Model pins: `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL` set which model each alias resolves to; Foundry needs them to match your deployment names. https://code.claude.com/docs/en/microsoft-foundry (step 4), https://code.claude.com/docs/en/env-vars. A FABLE variant was not found on the pages read; the user's setup has one (not verified).

## 4. Recommended way to run two accounts, and pitfalls
- Documented recommendation: separate config dirs, e.g. `alias claude-work='CLAUDE_CONFIG_DIR=~/.claude-work claude'`; each dir has its own settings, history and claude.ai login or API key. https://code.claude.com/docs/en/authentication ("Log in with multiple accounts"). The docs do not document the one-dir/env-switch approach as a pattern (see section 2: it follows from precedence).
- Pitfalls:
  - Separate dirs lose plugins, skills, hooks, memory, MCP servers, settings; nothing syncs between them (sections 1). Symlinks to share them are not documented; do not rely on them. Claude Code writes to these dirs (plugin installs, `~/.claude.json` rewrites), so shared symlinks could be clobbered (inferred).
  - A Foundry var left exported in the shell, or in settings `env`, keeps selecting Foundry and hides the claude.ai login (precedence rank 1). Settings `env` also beats the shell, so check both places.
  - `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` left in the environment outrank the subscription login. https://code.claude.com/docs/en/authentication ("Authentication precedence")
  - Pinned `ANTHROPIC_DEFAULT_*_MODEL` values hold Foundry deployment names; if left set in subscription mode they would point aliases at models that account may not serve (inferred). Keep them only in Foundry mode.
  - Features needing a claude.ai login (connectors, synced skills/plugins, `/schedule`) are unavailable on Foundry, and feature-flag fetching is skipped on third-party providers. https://code.claude.com/docs/en/env-vars ("Features that need feature-flag fetching"); https://code.claude.com/docs/en/authentication
  - Shared dir means shared transcripts, history and auto memory between work and personal sessions (inferred): company content ends up in the personal-account dir. Transcripts are plaintext. https://code.claude.com/docs/en/claude-directory ("Plaintext storage")
  - If managed settings set `forceLoginMethod`/`forceLoginOrgUUID`, a personal login can be refused. https://code.claude.com/docs/en/authentication ("Restrict login to your organization"). Whether your company sets this: not known; `/status` shows the managed source.

## Not documented / unverified
- That a single config dir holding a claude.ai login keeps it intact while Foundry env vars are active (logically follows from precedence; not stated).
- That `CLAUDE_CODE_USE_FOUNDRY` / `ANTHROPIC_FOUNDRY_*` work from a settings `env` block or `--settings` (generic `env` rule documented; Foundry page silent).
- Whether `"CLAUDE_CODE_USE_FOUNDRY": ""` counts as unset (documented only for Vertex as an example).
- Sharing skills/plugins/hooks between two config dirs via symlink or copy: not documented.
- `ANTHROPIC_DEFAULT_FABLE_MODEL`: not seen in the pages read. The `env` entry exceptions and "Variables Claude Code ignores in env" in settings-reference were not read in full (page truncated).
- Exact Keychain service naming: not documented (and not inspected, per the task rules).

## What this means for our setup
Recommended: ONE config dir (`~/.claude`), switch by environment per launch. It keeps all plugins, skills, hooks, MCP, memory. Costs: work and personal sessions share history/memory; relies on inferred (not documented) behavior in "Not documented", so test it.

Trade-offs vs two dirs: two dirs = documented, clean isolation of data and logins, but you must re-create and maintain plugins/skills/hooks/MCP/memory twice (the current problem). One dir = no duplication, weaker separation.

Setup (one-time):
1. Remove the exports from ~/.zshrc: CLAUDE_CODE_USE_FOUNDRY, ANTHROPIC_FOUNDRY_RESOURCE, ANTHROPIC_DEFAULT_{SONNET,HAIKU,OPUS,FABLE}_MODEL. Also ensure none are in `~/.claude/settings.json` `env`.
2. With them unset, run `claude auth login` (claude.ai) once for the default dir. `claude auth status` should then show claude.ai / firstParty.
3. Put the Foundry values in a wrapper in ~/.zshrc (function, not exports), so they only exist for that process:
   `claude-work() { CLAUDE_CODE_USE_FOUNDRY=1 ANTHROPIC_FOUNDRY_RESOURCE=<resource> ANTHROPIC_DEFAULT_SONNET_MODEL=<..> ANTHROPIC_DEFAULT_HAIKU_MODEL=<..> ANTHROPIC_DEFAULT_OPUS_MODEL=<..> ANTHROPIC_DEFAULT_FABLE_MODEL=<..> command claude "$@"; }`
   Alternative: the same keys in an `env` block of a file such as `~/.claude/foundry.settings.json`, run as `claude --settings ~/.claude/foundry.settings.json` (keeps the resource name out of zshrc; env-in-`--settings` is inferred to work).
4. Verify: `claude` then `/status` = claude.ai subscription; `claude-work` then `/status` = Microsoft Foundry.

Mode summary:
- Personal (claude.ai): set none of CLAUDE_CODE_USE_FOUNDRY, ANTHROPIC_FOUNDRY_*, ANTHROPIC_DEFAULT_*_MODEL, ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, CLAUDE_CODE_OAUTH_TOKEN. Run plain `claude`.
- Company (Foundry): set CLAUDE_CODE_USE_FOUNDRY=1, ANTHROPIC_FOUNDRY_RESOURCE, the four ANTHROPIC_DEFAULT_*_MODEL; Foundry auth via `az login` (leave ANTHROPIC_FOUNDRY_API_KEY and ANTHROPIC_FOUNDRY_AUTH_TOKEN unset) or an API key.
- If the one-dir test fails (login lost, or a stale var wins), fall back to `~/.claude-subscription` for personal and re-create the config you need there.
