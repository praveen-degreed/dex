# Contributing

Changes come in as pull requests; nobody pushes to `main` directly.

1. Fork, then `make setup` (venv, dependencies, config files from the examples).
2. Make your change and run `make test` (nine unit-test files, no network, no cost). Add or update a test for what you change.
3. Open a pull request that says what changed and why.

## Rules
- **No secrets and no personal setup in commits.** Tokens, Slack ids, real repo names, local paths and teammates' names belong in the
  ignored local files (`.env`, `people.json`, `policy.json`, `repos_extra.json`, `claude_profiles.json`), never in code, tests or examples.
  Examples use `your-org/your-repo` and tests use `acme/...`.
- **`dex/tools.py` is the safety boundary** (which commands a review or an answer may run). A change there needs a test, and
  `tests/check_permissions.py` should be run after it. That script makes real model calls and costs money (about $1 to $3), so it is not part of `make test`.
- `tests/eval_router.py` also calls the real model (about $0.05); run it when you change the router prompt.
- Keep the README in step with behaviour you change.
