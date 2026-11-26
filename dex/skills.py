"""The review skills Dex runs (`/review-prs`, `/review-prs-be`, `/review-prs-net`): find which ones the configured repos need,
install the missing ones from the public skills repo, and check that each configured repo is usable.

    python -m dex.skills check      what is installed where, and which configured repos have a problem
    python -m dex.skills install    install what is missing (never overwrites)
    python -m dex.skills update     reinstall every needed skill from SKILLS_REF (the installer keeps one backup)

Settings (.env, all optional): SKILLS_REPO (owner/name of the public repo), SKILLS_REF (branch or tag, default main),
SKILLS_AUTO_INSTALL (1 = install missing skills at startup, the default).

How: a shallow `git clone` of the public repo into <data>/skills-src (no GitHub login needed), then that repo's own
`install.sh <fe|be|net>` with REVIEW_SKILLS_SRC pointing at the clone and CLAUDE_HOME set to each Claude config folder
(~/.claude and every `config_dir` of claude_profiles.json that exists), so all accounts get the same skills.
This runs a script from SKILLS_REPO on this machine: point it at a repo you control and pin SKILLS_REF to a tag.
"""
import os
import subprocess
import sys
from pathlib import Path

KINDS = {"review-prs": "fe", "review-prs-be": "be", "review-prs-net": "net"}  # skill folder -> installer argument
DEFAULT_REPO = "your-org/review-skills"
TIMEOUT_S = 180


def settings():
    return (os.environ.get("SKILLS_REPO") or DEFAULT_REPO, os.environ.get("SKILLS_REF") or "main",
            os.environ.get("SKILLS_AUTO_INSTALL", "1") != "0")


def skill_name(skill):
    """'/review-prs-net {url}' -> 'review-prs-net'."""
    parts = (skill or "").split()
    return parts[0].lstrip("/") if parts else ""


def needed(repos):
    """The skill folders the reviewable repos call, e.g. {'review-prs': ['acme/web'], 'review-prs-be': ['acme/api']}."""
    out = {}
    for repo, cfg in repos.items():
        name = skill_name(cfg.get("skill"))
        if name:
            out.setdefault(name, []).append(repo)
    return out


def claude_homes():
    """Every Claude config folder that should hold the skills: ~/.claude, plus each profile's config_dir that exists."""
    from . import claude_env
    homes = [Path.home() / ".claude"]
    for prof in claude_env.profiles().values():
        if prof.get("config_dir"):
            h = Path(str(prof["config_dir"])).expanduser()
            if h.is_dir() and h not in homes:
                homes.append(h)
    return homes


def installed(home, name):
    return (home / "skills" / name / "SKILL.md").is_file()


def missing(repos, homes=None):
    """[(claude_home, skill)] for every skill a configured repo needs that a Claude folder lacks."""
    homes = homes or claude_homes()
    return [(h, n) for n in needed(repos) for h in homes if not installed(h, n)]


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT_S)


def fetch_source(data_dir, repo, ref):
    """Shallow-clone (or refresh) the skills repo into <data>/skills-src; returns the folder. Raises RuntimeError with the reason."""
    dest = Path(data_dir) / "skills-src"
    url = f"https://github.com/{repo}.git"
    if (dest / ".git").is_dir():
        r = _git("fetch", "--depth", "1", "origin", ref, cwd=dest)
        if r.returncode == 0:
            r = _git("checkout", "-f", "FETCH_HEAD", cwd=dest)
        if r.returncode != 0:
            raise RuntimeError(f"could not update {repo}@{ref}: {(r.stderr or r.stdout).strip()[:200]}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = _git("clone", "--depth", "1", "--branch", ref, url, str(dest))
    if r.returncode != 0:
        raise RuntimeError(f"could not clone {url} ({ref}): {(r.stderr or r.stdout).strip()[:200]}")
    return dest


def run_installer(src, home, name):
    kind = KINDS.get(name)
    if not kind:
        raise RuntimeError(f"unknown skill '{name}' (known: {', '.join(KINDS)}); install it yourself")
    env = {**os.environ, "REVIEW_SKILLS_SRC": str(src), "CLAUDE_HOME": str(home)}
    r = subprocess.run(["bash", str(Path(src) / "install.sh"), kind], env=env, capture_output=True, text=True, timeout=TIMEOUT_S)
    if r.returncode != 0:
        raise RuntimeError(f"installer failed for {name} in {home}: {(r.stderr or r.stdout).strip()[-200:]}")


def install(repos, data_dir, update=False, log=print):
    """Install the missing skills (or every needed one with update=True). Returns the problems, [] when all went well."""
    homes = claude_homes()
    todo = [(h, n) for n in needed(repos) for h in homes if update or not installed(h, n)]
    if not todo:
        return []
    repo, ref, _ = settings()
    problems = []
    try:
        src = fetch_source(data_dir, repo, ref)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        return [str(e)]
    for home, name in todo:
        try:
            run_installer(src, home, name)
            log(f"installed {name} into {home}/skills")
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            problems.append(str(e))
    return problems


def repo_problems(repos, homes=None):
    """{repo: reason} for configured repos that cannot be reviewed: the clone is not a git repo, or the skill is not installed."""
    homes = homes or claude_homes()
    out = {}
    for repo, cfg in repos.items():
        clone = Path(str(cfg.get("clone", ""))).expanduser()
        if not (clone / ".git").exists():
            out[repo] = f"clone path {clone} is not a git repository"
            continue
        name = skill_name(cfg.get("skill"))
        lacking = [str(h) for h in homes if name and not installed(h, name)]
        if not name:
            out[repo] = "no skill configured"
        elif lacking:
            out[repo] = f"skill /{name} is not installed in {', '.join(lacking)}"
    return out


def ensure(config, log):
    """Startup: install missing skills (when SKILLS_AUTO_INSTALL is on), then disable, with a clear log line, every repo that still
    cannot be reviewed. Never raises: a problem here must not stop the bot from answering questions."""
    try:
        _, _, auto = settings()
        if auto:
            for p in install(config.REPOS, config.DATA, log=log.info):
                log.warning("skills: %s", p)
        for repo, why in repo_problems(config.REPOS).items():
            log.warning("review of %s is disabled: %s", repo, why)
            config.REPOS.pop(repo, None)
            config.ALL_REPOS.pop(repo, None)
        if not config.REPOS:
            log.warning("no reviewable repos configured: Dex answers questions only (set REPOS in .env or repos_extra.json)")
    except Exception as e:  # noqa: BLE001
        log.warning("skills check failed: %s", e)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "check"
    if cmd not in ("check", "install", "update"):
        print(__doc__)
        return 2
    os.environ.setdefault("OWNER_USER_ID", "")  # setup runs before .env is filled in
    from . import config
    repos = config.REPOS
    if not repos:
        print("No reviewable repos configured (REPOS in .env, or repos_extra.json): nothing to install.")
        return 0
    if cmd in ("install", "update"):
        problems = install(repos, config.DATA, update=(cmd == "update"))
        for p in problems:
            print(f"problem: {p}")
    rc = 0
    for name, names in needed(repos).items():
        for h in claude_homes():
            ok = installed(h, name)
            rc |= 0 if ok else 1
            print(f"{'ok     ' if ok else 'MISSING'} /{name} in {h}/skills (for {', '.join(names)})")
    for repo, why in repo_problems(repos).items():
        rc = 1
        print(f"PROBLEM {repo}: {why}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
