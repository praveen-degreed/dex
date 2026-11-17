"""The PR review flow: validate and queue a review, then run the skill in a worktree pinned to the PR head.

The review skill itself decides and posts the review to GitHub; this module launches it, watches it, and
reports "started" and "done" in Slack.
"""
import datetime
import logging
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from . import config, github, progress, services, state
from . import slack_utils as su
from .claude_runner import BudgetExceeded, Stopped, run_claude
from .tools import review_tools

log = logging.getLogger("dex")
pool = ThreadPoolExecutor(max_workers=config.CONCURRENCY)


def prompt(skill, url, sha, post, tmp=None):
    lessons = config.LESSONS.read_text() if config.LESSONS.exists() else ""
    if post:
        mode = ("Post your review to the PR yourself, exactly as the skill normally does. The requester tagged the "
                "owner for this review, so the confirmation step is satisfied. Never merge, push or commit. "
                + (f"To post: first write the review body to {tmp}/review-body.md with the Write tool, then run "
                   f"`gh pr review <number> --repo <owner/repo> --comment|--approve|--request-changes --body-file {tmp}/review-body.md` "
                   "(one command). Never pass a multi-line body inline with --body or a heredoc: a body with a line that starts "
                   "with # is denied. " if tmp else ""))
    else:
        mode = ("DRAFT MODE: do NOT post anything to GitHub (no comments, reviews, approvals). Output the complete "
                "review as Markdown (category scoreboard, things to address, final verdict line), with no preamble.")
    return (
        f"{skill} {url}\n\n"
        f"The PR head is pinned at {sha}; review exactly that commit. {mode} "
        "Note: checks.txt from fetch-pr.sh lists only FAILING checks; an empty file means no check is failing, "
        "it does not mean CI could not be read. This run only allows single, simple commands: issue one command per Bash "
        "call, never chain with ; && || or pipes, and never use cd, shell variables, ~ or ls. Call the skill's scripts by "
        "their full absolute path (not ~/...); they find the repos themselves. A chained or non-matching call is denied "
        "as a whole.\n\n"
        f"Reviewer's standing lessons:\n{lessons}"
    )


def job_tokens(key):
    with state.lock:
        return dict((state.jobs.get(key) or {}).get("tokens") or {})


def start_review(channel, thread_ts, msg_ts, user, match, force):
    """Validate the PR, claim it, count the request against the user's limits, and queue the review."""
    url, repo, number = match.group(0), match.group(1).lower(), match.group(2)
    cfg = config.REPOS.get(repo)
    if not cfg:
        su.reply(channel, thread_ts, f"I don't review `{repo}` (it isn't in my configured repos).")
        return su.dm(config.OWNER, f"Review request for `{repo}` #{number}: no skill configured, skipped.")
    try:
        info = github.pr_info(url)
    except RuntimeError as err:
        return su.dm(config.OWNER, f"Could not read {url}: {err}")
    sha = info["sha"]
    if info["state"] != "OPEN":
        return su.reply(channel, thread_ts, f"{url} is {info['state'].lower()}; skipping.")

    cap = services.policy.max_pr_files()
    if cap and info.get("files", 0) > cap and not (force and user == config.OWNER):
        services.policy.audit(user=user, capability="review", channel=channel, thread=thread_ts, pr=url, decision="refused_size", files=info.get("files"))
        return su.reply(channel, thread_ts, f"{url} changes {info['files']} files, and reviews are limited to {cap} files right now because big reviews are expensive. The owner can override this.")

    key = f"{repo}#{number}@{sha}"
    with state.lock:
        if key in state.inflight:
            state.threads[(channel, thread_ts)] = key  # status replies in this thread follow the running review
            return su.reply(channel, thread_ts, f"A review of {url} at `{sha[:7]}` is already running. "
                                                "Ask for its status any time.")
    if not state.claim(key, force):
        return su.reply(channel, thread_ts, f"Already reviewed {url} at `{sha[:7]}`. Tell me to re-run it if you want a new review.")
    ok, why = services.policy.acquire(user, "review")
    if not ok:
        state.release(key)
        return su.reply(channel, thread_ts, why)
    state.new_job(key, url=url, sha=sha, repo=repo, user=user)
    with state.lock:
        state.threads[(channel, thread_ts)] = key
    pool.submit(review, url, repo, number, sha, key, cfg, channel, thread_ts, msg_ts, user, info["is_fork"])
    services.policy.audit(user=user, capability="review", channel=channel, thread=thread_ts, pr=url, sha=sha[:7], decision="started")
    su.react(channel, msg_ts, "eyes")
    su.reply(channel, thread_ts, f"Review started on {url} at `{sha[:7]}`. Ask for status or tell me to stop at any time.")


def review(url, repo, number, sha, key, cfg, channel, thread_ts, msg_ts, user, is_fork):
    if key in state.cancelled:
        state.cancelled.discard(key)
        state.release(key)
        state.update_job(key, status="stopped", finished_at=time.time())
        state.finish_job(key)
        return su.reply(channel, thread_ts, "Cancelled before it started.")
    post = not is_fork  # the skill posts itself, except on untrusted forks (draft only, shown in Slack)
    name = f"{repo.replace('/', '_')}-{number}-{sha[:7]}"
    wt, tmp = config.WORKTREES / name, config.TMPS / name
    tmp.mkdir(exist_ok=True)
    allowed, denied = review_tools(tmp, post)
    max_usd = services.policy.max_job_usd("review")
    draft = config.DRAFTS / f"{name}.md"
    raw, err = config.DRAFTS / f"{name}.jsonl", config.DRAFTS / f"{name}.err"
    clone = cfg["clone"]
    started = datetime.datetime.now(datetime.timezone.utc)
    state.update_job(key, status="running", started_at=time.time(), last_event=time.time(), activity="getting the PR ready")
    cost = 0.0
    try:
        subprocess.run(["git", "-C", clone, "fetch", "origin", f"pull/{number}/head"], check=True, capture_output=True)
        subprocess.run(["git", "-C", clone, "worktree", "add", "--detach", str(wt), sha], check=True, capture_output=True)
        result = run_claude(
            ["claude", "-p", prompt(cfg["skill"], url, sha, post, tmp),
             "--output-format", "stream-json", "--verbose",
             "--permission-mode", "dontAsk", "--allowedTools", allowed, "--disallowedTools", denied]
            + (["--max-budget-usd", f"{max_usd:g}"] if max_usd else []),
            wt, {**os.environ, "TMPDIR": f"{tmp}/"},  # trailing slash: fetch-pr.sh concatenates names onto it
            raw, err, key=key, stop_key=(channel, thread_ts),
        )
        cost = result.get("total_cost_usd") or 0.0
        draft.write_text(result.get("result") or "")
        denials = result.get("permission_denials") or []
        if denials:
            def shown(d):
                inp = d.get("tool_input") or {}
                return f"- {d.get('tool_name')}: {str(inp.get('command') or inp.get('file_path') or inp)[:160]}"
            log.warning("%d tool call(s) denied for %s", len(denials), key)
            su.dm(config.OWNER, f"Review of {url}: {len(denials)} tool call(s) were denied by the bot's permission rules:\n"
                  + "\n".join(shown(d) for d in denials[:8]))
        link = ""
        if post:
            found = github.posted_review(repo, number, started)
            if found:
                gh_state, link = found
                outcome = github.REVIEW_LABEL.get(gh_state, gh_state.lower())
                su.reply(channel, thread_ts, f"Review done on {url}: {outcome}. {link}")
                done = "white_check_mark"
            else:
                outcome = "finished, but no posted review was found"
                su.reply(channel, thread_ts, f"Review finished but I could not find a posted review on {url}; check the PR.")
                done = "warning"
        else:
            body = draft.read_text().strip()
            if not body:
                raise RuntimeError("review produced no output")
            outcome = "not posted (PR is from a fork)"
            su.reply(channel, thread_ts, f"Review done on {url}, not posted (PR is from a fork).\n\n{body[:2800]}")
            done = "white_check_mark"
        state.update_job(key, status="done", outcome=outcome, link=link, finished_at=time.time())
        su.react_remove(channel, msg_ts, "eyes")
        su.react(channel, msg_ts, done)
        if done == "white_check_mark" and not denials and outcome != github.REVIEW_LABEL.get("COMMENTED", "comments posted"):
            raw.unlink(missing_ok=True)  # keep the raw event log when the run failed, was denied something, posted nothing or
            # was comment-only (the skill downgrades when a reviewer did not finish: the log shows why)
        services.policy.audit(user=user, capability="review", channel=channel, thread=thread_ts, pr=url, sha=sha[:7],
                              decision="done", outcome=outcome, usd=round(cost, 4), denials=len(denials), tokens=job_tokens(key))
    except BudgetExceeded as e:
        cost = e.cost
        state.release(key)
        state.update_job(key, status="stopped", outcome="stopped at the budget", finished_at=time.time())
        su.react_remove(channel, msg_ts, "eyes")
        su.react(channel, msg_ts, "octagonal_sign")
        su.reply(channel, thread_ts, f"Stopped the review of {url}: it reached the ${max_usd:g} per-review limit (about ${cost:.2f} spent) before finishing, so nothing was posted. Ask the owner to raise the limit, or review a smaller PR.")
        su.dm(config.OWNER, f"The review of {url} was stopped at the ${max_usd:g} per-review limit (spent about ${cost:.2f}).")
        services.policy.audit(user=user, capability="review", channel=channel, thread=thread_ts, pr=url, decision="budget_stopped", usd=round(cost, 4), tokens=job_tokens(key))
    except Stopped:
        state.release(key)
        state.update_job(key, status="stopped", finished_at=time.time())
        su.react_remove(channel, msg_ts, "eyes")
        su.react(channel, msg_ts, "octagonal_sign")
        su.reply(channel, thread_ts, f"Stopped the review of {url}.")
        services.policy.audit(user=user, capability="review", channel=channel, thread=thread_ts, pr=url, decision="stopped")
    except Exception as err_:
        state.release(key)  # allow a retry on this SHA
        state.update_job(key, status="failed", finished_at=time.time())
        su.reply(channel, thread_ts, f"Review of {url} failed.")
        su.react_remove(channel, msg_ts, "eyes")
        su.react(channel, msg_ts, "x")
        su.dm(config.OWNER, f"Review of {url} failed: {str(getattr(err_, 'stderr', None) or err_)[:500]}")
        services.policy.audit(user=user, capability="review", channel=channel, thread=thread_ts, pr=url, decision="failed",
                              error=str(err_)[:200])
    finally:
        services.policy.add_cost(user, cost, job_tokens(key))
        state.finish_job(key)
        r = subprocess.run(["git", "-C", clone, "worktree", "remove", "--force", str(wt)], capture_output=True)
        if r.returncode:
            shutil.rmtree(wt, ignore_errors=True)
        if tmp.parent == config.TMPS:  # only ever delete this review's own folder under tmp/
            shutil.rmtree(tmp, ignore_errors=True)
        # Prune AFTER the delete: worktrees the skill created inside tmp/ are only prunable once their dir is gone.
        subprocess.run(["git", "-C", clone, "worktree", "prune"], capture_output=True)
