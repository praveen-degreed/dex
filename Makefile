# Dex: one-line entry points. Each target wraps code that `dexctl` and the bot itself use, so there is one implementation.
#   make setup    first time on a machine
#   make doctor   what is ready and what is still yours to do
#   make run      start the bot
.PHONY: help setup venv files skills skills-update doctor run test

PY := .venv/bin/python

help:
	@echo "make setup          venv + dependencies + config files from the examples + review skills"
	@echo "make skills         install the review skills the configured repos need (only what is missing)"
	@echo "make skills-update  reinstall those skills from SKILLS_REF"
	@echo "make doctor         check gh, claude, config and skills; says what is left to do"
	@echo "make run            start the bot"
	@echo "make test           unit tests (no network, no cost)"

venv: .venv/.stamp
.venv/.stamp: requirements.txt
	[ -x $(PY) ] || python3 -m venv .venv
	$(PY) -m pip install -q -r requirements.txt
	@touch .venv/.stamp

# Copies an example only when the real file is missing; never overwrites yours.
files:
	@[ -f .env ] || { cp env.example .env && echo "created .env: fill in the Slack tokens, your Slack id, your GitHub org and your repos"; }
	@[ -f people.json ] || { cp people.example.json people.json && echo "created people.json: replace the examples with your teammates (optional)"; }

setup: venv files skills
	@echo ""
	@echo "Still yours to do (Dex cannot do these for you):"
	@echo "  1. Create the Slack app (docs/slack-app.md) and put its two tokens and your Slack id in .env"
	@echo "  2. Put your repos in .env (REPOS) or repos_extra.json, and your GitHub org in .env (GITHUB_ORG)"
	@echo "  3. gh auth login     and     claude   (then /login)"
	@echo "  4. make doctor, then make run"

skills: venv
	@$(PY) -m dex.skills install

skills-update: venv
	@$(PY) -m dex.skills update

doctor: venv
	@echo "== gh";     gh auth status 2>&1 | head -3 || true
	@echo "== claude"; claude auth status 2>&1 | head -3 || true
	@echo "== config"; $(PY) -c "import os; from dotenv import load_dotenv; load_dotenv('.env'); [print(('ok      ' if os.environ.get(k) else 'MISSING ') + k) for k in ('SLACK_BOT_TOKEN','SLACK_APP_TOKEN','OWNER_USER_ID','GITHUB_ORG')]"
	@echo "== repos and review skills"; $(PY) -m dex.skills check; true

run: venv
	$(PY) -u bot.py

test: venv
	@fail=0; for t in tests/test_*.py; do if $(PY) $$t >/dev/null 2>&1; then echo "ok      $$t"; else echo "FAILED  $$t"; fail=1; fi; done; exit $$fail
