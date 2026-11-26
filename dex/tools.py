"""Tool allowlists and deny lists for the `claude -p` runs. This is the safety boundary: read here before widening.

Rule syntax notes (verified against Claude Code): `Edit(/<abs>/**)` also covers Write; a `Write(...)` rule is
rejected. A path rule needs `//` for an absolute path, which `f"Edit(/{abs_path}/**)"` produces.
"""
from pathlib import Path

_SKILLS = Path.home() / ".claude" / "skills"


def skill_roots():
    """Every folder the review skills may be installed in: the normal one plus the skills folder of each Claude profile that has its own
    config dir (the personal subscription: ~/.claude-subscription). A run on that profile calls its scripts from THERE, so the allowlist must
    cover it (it did not, and the denial showed up only when the skill's own tool grant lapsed late in a long run)."""
    from . import claude_env  # imported here: claude_env needs config, which tools.py otherwise does not
    roots = [_SKILLS]
    for prof in claude_env.profiles().values():
        if prof.get("config_dir"):
            r = Path(str(prof["config_dir"])).expanduser() / "skills"
            if r not in roots:
                roots.append(r)
    return roots
BOTQ = str(Path(__file__).resolve().parent.parent / "bin" / "botq")  # read-only helper commands for the assistant

def skill_names():
    """The skill folders whose scripts a review may run: the three shipped skills plus any skill a configured repo names."""
    names = {"review-prs", "review-prs-be", "review-prs-net"}
    try:
        from . import config
        names |= {c["skill"].split()[0].lstrip("/") for c in config.ALL_REPOS.values() if isinstance(c.get("skill"), str) and c["skill"].split()}
    except Exception:  # noqa: BLE001  (config needs the environment; without it only the shipped skills are allowed)
        pass
    return sorted(names)


REVIEW_READ = [
    "Read", "Grep", "Glob", "Skill", "Agent",
    "Bash(gh pr view:*)", "Bash(gh pr diff:*)", "Bash(gh pr checks:*)", "Bash(gh pr list:*)", "Bash(gh repo view:*)",
    "Bash(gh api repos/*/pulls/*/reviews)",  # exact form only, so `-X POST` cannot be appended
    "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git blame:*)",
    "Bash(git grep:*)", "Bash(git ls-files:*)", "Bash(git status:*)",
    # the same read-only commands with `git -C <dir>`: only these subcommands, so `git -C <dir> push` or `commit` stay unallowed
    "Bash(git -C * grep *)", "Bash(git -C * log *)", "Bash(git -C * show *)", "Bash(git -C * diff *)", "Bash(git -C * blame *)",
    "Bash(git -C * ls-files *)", "Bash(git -C * rev-parse *)", "Bash(git -C * status *)",
    # `/*`, not `/:*`: the `:*` form needs a space after the prefix, so it never matched `.../scripts/name.sh` (verified).
    *[f"Bash({root}/{name}/scripts/*)" for root in skill_roots() for name in skill_names()],
    "Bash(git rev-parse:*)", "Bash(git fetch:*)", "Bash(cat:*)",
    # Read-only helpers the skills chain with the commands above (`cd x && script`, `ls | head`). A chained call is allowed
    # only when every part matches a rule, and paths must stay inside the run's directory (verified: tests/check_permissions.py).
    "Bash(cd:*)", "Bash(ls:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)", "Bash(pwd)", "Bash(grep *)", "Bash(rg *)", "Bash(jira issue view *)",
]
REVIEW_POST = ["Bash(gh pr review:*)", "Bash(gh pr comment:*)"]  # added only when the skill may post
REVIEW_DENIED = ["Bash(gh pr merge:*)", "Bash(git push:*)", "Bash(git commit:*)", "Bash(git -C * push *)", "Bash(git -C * commit *)", "Bash(git -C * checkout *)", "Bash(git -C * reset *)", "Bash(*/../*)"]  # last: no path climbing out of the scripts dirs


def review_tools(tmp, post):
    """(allowed, denied) strings for a review run. Writes are limited to that run's own temp dir."""
    allowed = REVIEW_READ + [f"Edit(/{tmp}/**)"] + (REVIEW_POST if post else [])
    return " ".join(allowed), " ".join(REVIEW_DENIED)


GH_READ = ["gh pr view", "gh pr list", "gh pr diff", "gh pr checks", "gh search prs", "gh search commits", "gh search issues"]
QA_DENIED = [
    "Edit", "Write", "NotebookEdit", "Read", "Grep", "Glob",
    "Bash(git push:*)", "Bash(git commit:*)", "Bash(git checkout:*)", "Bash(git reset:*)", "Bash(gh pr merge:*)",
    "Bash(gh pr review:*)", "Bash(gh pr comment:*)",
    "Read(**/.env)", "Read(**/.env.*)", "Read(**/*.pem)", "Read(**/*.key)", "Read(**/id_rsa*)", "Read(**/*secret*)",
]


def qa_tools():
    """(allowed, denied) for the assistant. It has no file-read, search or git tools at all: everything it knows about
    code comes from `botq` (which reads origin/<branch> of the configured clones only) and read-only `gh`."""
    allowed = [f"Bash({BOTQ}:*)"] + [f"Bash({g}:*)" for g in GH_READ]
    return " ".join(allowed), " ".join(QA_DENIED)
