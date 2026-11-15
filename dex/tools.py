"""Tool allowlists and deny lists for the `claude -p` runs. This is the safety boundary: read here before widening.

Rule syntax notes (verified against Claude Code): `Edit(/<abs>/**)` also covers Write; a `Write(...)` rule is
rejected. A path rule needs `//` for an absolute path, which `f"Edit(/{abs_path}/**)"` produces.
"""
from pathlib import Path

_SKILLS = Path.home() / ".claude" / "skills"
BOTQ = str(Path(__file__).resolve().parent.parent / "bin" / "botq")  # read-only helper commands for the assistant

REVIEW_READ = [
    "Read", "Grep", "Glob", "Skill", "Agent",
    "Bash(gh pr view:*)", "Bash(gh pr diff:*)", "Bash(gh pr checks:*)", "Bash(gh pr list:*)",
    "Bash(gh api repos/*/pulls/*/reviews)",  # exact form only, so `-X POST` cannot be appended
    "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git blame:*)",
    "Bash(git grep:*)", "Bash(git ls-files:*)", "Bash(git status:*)",
    f"Bash({_SKILLS}/review-prs/scripts/:*)", f"Bash({_SKILLS}/review-prs-be/scripts/:*)",
    "Bash(git rev-parse:*)", "Bash(git fetch:*)", "Bash(cat:*)",
]
REVIEW_POST = ["Bash(gh pr review:*)", "Bash(gh pr comment:*)"]  # added only when the skill may post
REVIEW_DENIED = ["Bash(gh pr merge:*)", "Bash(git push:*)", "Bash(git commit:*)"]


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
