"""The PR review flow: validate and queue a review, then run the skill in a worktree pinned to the PR head.

The review skill itself decides and posts the review to GitHub; this module launches it, watches it, and
reports "started" and "done" in Slack.
"""
import datetime
import json
import logging
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from . import claude_env, config, github, progress, services, state
from . import slack_utils as su
from .claude_runner import BudgetExceeded, Stopped, run_claude
from .tools import review_tools

log = logging.getLogger("dex")
pool = ThreadPoolExecutor(max_workers=config.CONCURRENCY)


def choose_skill(cfg, paths):
    """The skill for a PR: the repo's maestro_skill when a changed path matches maestro_paths (case-insensitive,
    default "maestro|aicoach"), else its normal skill. Repos without maestro_skill always use `skill`."""
    ms = cfg.get("maestro_skill")
    if ms and any(re.search(cfg.get("maestro_paths") or "maestro|aicoach", p, re.I) for p in paths):
        return ms
    return cfg["skill"]


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
    number = url.rstrip("/").rsplit("/", 1)[-1]
    head = skill.format(number=number, url=url) if "{" in skill else f"{skill} {url}"  # a skill string may carry its own arguments
    return (
        f"{head}\n\n"
        f"The PR head is pinned at {sha}; review exactly that commit. {mode} "
        "Note: checks.txt from fetch-pr.sh lists only FAILING checks; an empty file means no check is failing, "
        "it does not mean CI could not be read. This run only allows single, simple commands: issue one command per Bash "
        "call, never chain with ; && || or pipes, and never use cd, shell variables, ~ or ls. Call the skill's scripts by "
        "their full absolute path (not ~/...); they find the repos themselves. A chained or non-matching call is denied "
        "as a whole.\n\n"
        f"Reviewer's standing lessons:\n{lessons}"
    )


VERDICT_LINE = re.compile(r"^[ \t]*DEX_VERDICT:[ \t]*(approve|request_changes|comment)[ \t]*$", re.M | re.I)


def split_verdict(text):
    """(verdict, text without the verdict line). The review skills end a drafted review with `DEX_VERDICT: approve|request_changes|comment`;
    no line (or an unknown word) means comment."""
    found = VERDICT_LINE.findall(text)
    verdict = found[-1].lower() if found else "comment"
    return verdict, VERDICT_LINE.sub("", text).rstrip()


def count_agent_calls(raw):
    """How many review agents the orchestrator launched, from the saved event log (None if there is no log)."""
    try:
        text = raw.read_text()
    except OSError:
        return None
    n = 0
    for line in text.splitlines():
        if '"tool_use"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") != "assistant" or ev.get("parent_tool_use_id"):
            continue
        n += sum(1 for c in (ev.get("message") or {}).get("content") or []
                 if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") in ("Agent", "Task"))
    return n


def _n(n):
    return f"{n / 1e6:.2f}M" if n >= 1e6 else (f"{n / 1e3:.0f}K" if n >= 1e3 else str(int(n)))


def usage_line(key, cost, agents, started, profile):
    """One line for the end of a review: tokens, cost, agents, time and the account it ran on ('' if nothing was used)."""
    t = job_tokens(key)
    fresh, cached, out = t.get("fresh", 0), t.get("cache_read", 0), t.get("output", 0)
    total = fresh + cached + out
    if not total and not cost:
        return ""
    mins = max(0, int((datetime.datetime.now(datetime.timezone.utc) - started).total_seconds()))
    account = "Azure Foundry" if profile == claude_env.FOUNDRY else f"{profile} (personal plan)"
    price = f"${cost:.2f}" + ("" if profile == claude_env.FOUNDRY else " (API-equivalent estimate, not billed per token)")
    parts = [f"{_n(total)} tokens ({_n(fresh)} new, {_n(cached)} cached, {_n(out)} output)", price]
    if agents is not None:
        parts.append(f"{agents} agent{'s' if agents != 1 else ''}")
    parts.append(f"{mins // 60}m {mins % 60:02d}s" if mins >= 60 else f"{mins}s")
    parts.append(f"account: {account}")
    return "\n_Usage: " + " · ".join(parts) + "_"


def remove_worktree(clone, wt):
    """Remove one review's worktree folder (and git's record of it). Only ever touches a folder directly under worktrees/."""
    if wt.parent != config.WORKTREES:
        return
    r = subprocess.run(["git", "-C", clone, "worktree", "remove", "--force", str(wt)], capture_output=True)
    if r.returncode:
        shutil.rmtree(wt, ignore_errors=True)
    subprocess.run(["git", "-C", clone, "worktree", "prune"], capture_output=True)


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

    if cfg.get("maestro_skill") or cfg.get("dex_posts"):  # repos whose skills live in the repo itself
        try:
            paths = github.pr_paths(url)
        except RuntimeError as err:
            return su.dm(config.OWNER, f"Could not read the files of {url}: {err}")
        if any(p.startswith(".claude/") for p in paths) and not (force and user == config.OWNER):
            services.policy.audit(user=user, capability="review", channel=channel, thread=thread_ts, pr=url, decision="refused_claude_dir")
            return su.reply(channel, thread_ts, f"{url} changes files under `.claude/` (skills, agents or hooks). This repo's review skills come from the PR "
                                                "itself, so a PR could rewrite its own reviewer; I won't run them. The owner can force it, or review it by hand.")
        cfg = {**cfg, "skill": choose_skill(cfg, paths)}
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
    dex_posts = bool(cfg.get("dex_posts")) and not is_fork  # the skill only drafts; Dex posts the draft as one comment-only review
    post = not is_fork and not dex_posts  # otherwise the skill posts itself, except on untrusted forks (draft only, shown in Slack)
    name = f"{repo.replace('/', '_')}-{number}-{sha[:7]}"
    wt, tmp = config.WORKTREES / name, config.TMPS / name
    tmp.mkdir(exist_ok=True)
    allowed, denied = review_tools(tmp, post)
    max_usd = services.policy.max_job_usd("review")
    draft = config.DRAFTS / f"{name}.md"
    raw, err = config.DRAFTS / f"{name}.jsonl", config.DRAFTS / f"{name}.err"
    clone = cfg["clone"]
    started = datetime.datetime.now(datetime.timezone.utc)
    profile = claude_env.profile_name(services.policy, "review")  # the account this review runs on, read once at the start
    agents = None
    state.update_job(key, status="running", started_at=time.time(), last_event=time.time(), activity="getting the PR ready")
    cost = 0.0
    try:
        subprocess.run(["git", "-C", clone, "fetch", "origin", f"pull/{number}/head"], check=True, capture_output=True)
        remove_worktree(clone, wt)  # a run that was killed leaves its folder behind; start from a clean one
        subprocess.run(["git", "-C", clone, "worktree", "add", "--detach", str(wt), sha], check=True, capture_output=True)
        env = claude_env.env_for(services.policy, "review", {"TMPDIR": f"{tmp}/"})  # trailing slash: fetch-pr.sh concatenates names onto it
        result = run_claude(
            ["claude", "-p", prompt(cfg["skill"], url, sha, post, tmp),
             "--output-format", "stream-json", "--verbose",
             "--permission-mode", "dontAsk", "--allowedTools", allowed, "--disallowedTools", denied]
            + (["--max-budget-usd", f"{max_usd:g}"] if max_usd else []),
            wt, env,
            raw, err, key=key, stop_key=(channel, thread_ts),
        )
        cost = result.get("total_cost_usd") or 0.0
        draft.write_text(result.get("result") or "")
        if dex_posts:
            body = (result.get("result") or "").strip()
            if not body:
                raise RuntimeError("the review skill produced no text to post")
            verdict, body = split_verdict(body)
            note = ""
            if verdict == "approve" and count_agent_calls(raw) == 0:  # never approve a review that no agent worked on
                verdict, note = "comment", " Downgraded from approve: no review agents ran."
            flags = {"approve": "--approve", "request_changes": "--request-changes", "comment": "--comment"}
            body_file = tmp / "review-body.md"
            body_file.write_text(body[:60000] + f"\n\n---\n:robot: AI-assisted review (`{cfg['skill'].split()[0].lstrip('/')}`), posted by Dex with the skill's decision: "
                                 f"{verdict.replace('_', ' ')}.{note} A human can dismiss it.\n")

            def gh_review(flag):
                return subprocess.run(["gh", "pr", "review", number, "--repo", repo, flag, "--body-file", str(body_file)], capture_output=True, text=True)
            posted = gh_review(flags[verdict])
            if posted.returncode and verdict != "comment" and "own pull request" in (posted.stderr or "").lower():
                posted = gh_review("--comment")  # GitHub does not let the author approve or request changes on their own PR
            if posted.returncode:
                raise RuntimeError(f"gh pr review failed: {(posted.stderr or '')[:200]}")
            post = True  # from here on it is looked up like a review the skill posted itself
        denials = result.get("permission_denials") or []
        if denials:
            def shown(d):
                inp = d.get("tool_input") or {}
                return f"- {d.get('tool_name')}: {str(inp.get('command') or inp.get('file_path') or inp)[:160]}"
            log.warning("%d tool call(s) denied for %s", len(denials), key)
            su.dm(config.OWNER, f"Review of {url}: {len(denials)} tool call(s) were denied by the bot's permission rules:\n"
                  + "\n".join(shown(d) for d in denials[:8]))
        agents = count_agent_calls(raw)
        no_agents = agents == 0
        usage = usage_line(key, cost, agents, started, profile)
        if no_agents:
            log.warning("review of %s ran no review agents", key)
            su.dm(config.OWNER, f"Review of {url} ran NO review agents: the skill reviewed alone. Treat it as a quick read. "
                  f"The raw log is kept in {raw.name}.")
        link = ""
        if post:
            found = github.posted_review(repo, number, started)
            if found:
                gh_state, link = found
                outcome = github.REVIEW_LABEL.get(gh_state, gh_state.lower())
                su.reply(channel, thread_ts, f"Review done on {url}: {outcome}. {link}"
                         + ("\n:warning: The skill ran no review agents (it reviewed alone), so treat this as a quick read, not a full review." if no_agents else "") + usage)
                done = "white_check_mark"
            else:
                outcome = "finished, but no posted review was found"
                su.reply(channel, thread_ts, f"Review finished but I could not find a posted review on {url}; check the PR." + usage)
                done = "warning"
        else:
            body = draft.read_text().strip()
            if not body:
                raise RuntimeError("review produced no output")
            outcome = "not posted (PR is from a fork)"
            su.reply(channel, thread_ts, f"Review done on {url}, not posted (PR is from a fork).\n\n{body[:2800]}" + usage)
            done = "white_check_mark"
        state.update_job(key, status="done", outcome=outcome, link=link, finished_at=time.time())
        su.react_remove(channel, msg_ts, "eyes")
        su.react(channel, msg_ts, done)
        if done == "white_check_mark" and not denials and not no_agents and outcome != github.REVIEW_LABEL.get("COMMENTED", "comments posted"):
            raw.unlink(missing_ok=True)  # keep the raw event log when the run failed, was denied something, posted nothing or
            # was comment-only (the skill downgrades when a reviewer did not finish: the log shows why)
        services.policy.audit(user=user, capability="review", channel=channel, thread=thread_ts, pr=url, sha=sha[:7],
                              decision="done", outcome=outcome, usd=round(cost, 4), denials=len(denials), agents=agents, profile=profile, tokens=job_tokens(key))
    except BudgetExceeded as e:
        cost = e.cost
        state.release(key)
        state.update_job(key, status="stopped", outcome="stopped at the budget", finished_at=time.time())
        su.react_remove(channel, msg_ts, "eyes")
        su.react(channel, msg_ts, "octagonal_sign")
        su.reply(channel, thread_ts, f"Stopped the review of {url}: it reached the ${max_usd:g} per-review limit (about ${cost:.2f} spent) before finishing, so nothing was posted. Ask the owner to raise the limit, or review a smaller PR." + usage_line(key, cost, agents, started, profile))
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
        su.reply(channel, thread_ts, f"Review of {url} failed." + usage_line(key, cost, agents, started, profile))
        su.react_remove(channel, msg_ts, "eyes")
        su.react(channel, msg_ts, "x")
        su.dm(config.OWNER, f"Review of {url} failed: {str(getattr(err_, 'stderr', None) or err_)[:500]}")
        services.policy.audit(user=user, capability="review", channel=channel, thread=thread_ts, pr=url, decision="failed",
                              error=str(err_)[:200])
    finally:
        services.policy.add_cost(user, cost, job_tokens(key))
        state.finish_job(key)
        remove_worktree(clone, wt)
        if tmp.parent == config.TMPS:  # only ever delete this review's own folder under tmp/
            shutil.rmtree(tmp, ignore_errors=True)
        # Prune AFTER the delete: worktrees the skill created inside tmp/ are only prunable once their dir is gone.
        subprocess.run(["git", "-C", clone, "worktree", "prune"], capture_output=True)
