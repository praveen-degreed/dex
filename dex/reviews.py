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
from .github import canonical
from .limits import limit_active, limit_text, note_limit
from . import slack_utils as su
from .claude_runner import BudgetExceeded, Stopped, UsageLimit, run_claude
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


def prompt(skill, url, sha, post, tmp=None, confirmed_big=False, rerun=False):
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
        + ("The requester explicitly asked to re-run this review on a commit that was already reviewed: do NOT stop because it is a duplicate; run the full review again. " if rerun else "")
        + ("The requester confirmed this PR cannot be split: review it fully, even above the skill's own size or unit limit, and do not stop to ask the author to split it. " if confirmed_big else "")
        + 
        "Note: checks.txt from fetch-pr.sh lists only FAILING checks; an empty file means no check is failing, "
        "it does not mean CI could not be read. This run only allows single, simple commands: issue one command per Bash "
        "call, never chain with ; && || or pipes, and never use cd, shell variables, ~ or ls. Call the skill's scripts by "
        "their full absolute path (not ~/...); they find the repos themselves. A chained or non-matching call is denied "
        "as a whole.\n\n"
        f"Reviewer's standing lessons:\n{lessons}"
    )


VERDICT_LINE = re.compile(r"^[ \t]*DEX_VERDICT:[ \t]*(approve|request_changes|comment|skip)[ \t]*$", re.M | re.I)


def split_verdict(text):
    """(verdict, text without the verdict line). The review skills end a drafted review with `DEX_VERDICT: approve|request_changes|comment|skip`
    (skip = nothing was reviewed, post nothing); no line (or an unknown word) means comment."""
    found = VERDICT_LINE.findall(text)
    verdict = found[-1].lower() if found else "comment"
    return verdict, VERDICT_LINE.sub("", text).rstrip()


SERIOUS = re.compile(r"\b(push|merge|rm|curl|wget|ssh|scp|sudo|chmod|chown|kill|docker|kubectl|az|aws|npm|pip|python3?|pytest|dotnet|node|git\s+(-C\s+\S+\s+)?(commit|checkout|reset|clean))\b|[^=<-]>\s*\S")


def serious_denials(denials):
    """The denied calls that are worth telling the owner about: anything that writes, publishes, runs PR code or reaches out.
    A harmless read the allowlist did not cover (ls, sed -n, a sibling repo lookup) is only counted and logged."""
    out = []
    for d in denials:
        inp = d.get("tool_input") or {}
        text = str(inp.get("command") or "")
        if d.get("tool_name") == "Bash" and SERIOUS.search(text):
            out.append(d)
    return out


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


AGAIN_HINT = ("\n\n:speech_balloon: _Addressed the comments? Ask me to review again right here in this thread "
              "(for example \"review again\") and I'll re-review it and check the earlier comments._")
OUTCOME_ICON = {"approved": ":white_check_mark:", "changes requested": ":x:", "comments posted": ":speech_balloon:"}


def done_message(user, url, outcome, link, notes, usage, hint):
    """The reply that ends a review: who it is for, the result with a link, any notes, the usage card, and the invitation to ask again."""
    label = url.replace("https://github.com/", "").replace("/pull/", " #")
    lines = [f"{OUTCOME_ICON.get(outcome, ':white_check_mark:')} <@{user}> Your review of <{url}|{label}> is done.",
             f"*Result*  {outcome.capitalize()}" + (f"  ·  <{link}|View the review>" if link else "")]
    lines += notes
    return "\n".join(lines) + usage + hint


def _n(n):
    return f"{n / 1e6:.2f}M" if n >= 1e6 else (f"{n / 1e3:.0f}K" if n >= 1e3 else str(int(n)))


def usage_line(key, cost, agents, started, profile, denied=0):
    """The usage card at the end of a review (Slack mrkdwn, shown as a quote): tokens, cost, agents, time, account. '' if nothing was used."""
    t = job_tokens(key)
    fresh, cached, out = t.get("fresh", 0), t.get("cache_read", 0), t.get("output", 0)
    total = fresh + cached + out
    if not total and not cost:
        return ""
    secs = max(0, int((datetime.datetime.now(datetime.timezone.utc) - started).total_seconds()))
    took = f"{secs // 60}m {secs % 60:02d}s" if secs >= 60 else f"{secs}s"
    own = profile == claude_env.FOUNDRY
    account = "Azure Foundry" if own else ("Personal Claude plan (subscription)" if profile == "subscription" else f"{profile} profile")
    cost_line = f"> *Cost*  `${cost:.2f}`" + ("" if own else "  _API-equivalent estimate, not billed per token_")
    lines = ["> :bar_chart: *Usage*",
             f"> *Tokens*  `{_n(total)}`  ({_n(fresh)} new · {_n(cached)} cached · {_n(out)} output)",
             cost_line]
    if agents is not None:
        lines.append(f"> *Agents*  `{agents}`      *Time*  `{took}`")
    else:
        lines.append(f"> *Time*  `{took}`")
    lines.append(f"> *Account*  {account}")
    if denied:
        lines.append(f"> :no_entry_sign: {denied} tool call{'s' if denied != 1 else ''} blocked by safety rules")
    return "\n\n" + "\n".join(lines)  # a blank line first, so the card stands apart from the sentence above it


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


def start_review(channel, thread_ts, msg_ts, user, match, force, confirmed=False):
    """Validate the PR, claim it, count the request against the user's limits, and queue the review."""
    url, repo, number = canonical(match), match.group(1).lower(), match.group(2)  # always the https form (Slack shows http:// links)
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
    files_lim, lines_lim = services.policy.big_pr()
    changed = info.get("additions", 0) + info.get("deletions", 0)
    is_big = bool((files_lim and info.get("files", 0) > files_lim) or (lines_lim and changed > lines_lim))
    if is_big and force and user == config.OWNER:
        confirmed = True  # the owner forcing a big PR is the answer to the question: review it fully
    if is_big and not confirmed:
        size = f"{info.get('files', 0)} files, +{info.get('additions', 0)}/-{info.get('deletions', 0)} lines"
        with state.lock:
            state.pending_big.setdefault((channel, thread_ts), []).append({"url": url, "user": user, "msg_ts": msg_ts, "at": time.time(), "size": size})
        services.policy.audit(user=user, capability="review", channel=channel, thread=thread_ts, pr=url, decision="asked_big", files=info.get("files", 0))
        return su.reply(channel, thread_ts, f"<@{user}> {url} is big ({size}). A smaller PR gets a better review and uses far fewer tokens: a review this size "
                                            "often costs several dollars and takes 10+ minutes, and an agent can only hold so much of it in mind. "
                                            "Can it be split? If so, send me the smaller PRs. If it can't be split (for example it is tested as one piece), "
                                            "just tell me and I'll review it fully.")
    if confirmed and is_big:
        cfg = {**cfg, "confirmed_big": True}  # the requester said it cannot be split: review it fully, even above the skill's own size limit
    profile = claude_env.profile_name(services.policy, "review")
    hit = limit_active(profile)
    if hit and not (force and user == config.OWNER):
        services.policy.audit(user=user, capability="review", channel=channel, thread=thread_ts, pr=url, decision="refused_limit", profile=profile)
        return su.reply(channel, thread_ts, f"<@{user}> I did not start the review of {url}. " + limit_text(profile, hit[1]).replace(" The owner can switch", " The owner can force it or switch")
                        + " Send it again after the reset.")
    key = f"{repo}#{number}@{sha}"
    with state.lock:
        if key in state.inflight:
            state.threads[(channel, thread_ts)] = key  # status replies in this thread follow the running review
            return su.reply(channel, thread_ts, f"A review of {url} at `{sha[:7]}` is already running. "
                                                "Ask for its status any time.")
    was_reviewed = key in state.seen  # read before the claim: a forced run on a reviewed commit is a re-run
    if not state.claim(key, force):
        found = github.review_at(repo, number, sha)
        with state.lock:
            state.awaiting_link.add((channel, thread_ts))  # my invitation to re-run: the next message here may be the answer, no mention needed
        existing = f" The review is here: <{found[1]}|{github.REVIEW_LABEL.get(found[0], found[0].lower()).capitalize()}>." if found else ""
        return su.reply(channel, thread_ts, f"Already reviewed <{url}> at `{sha[:7]}`.{existing} The code has not changed since, so I would find the same things. "
                                            "If you still want a fresh run, say so here (or push a new commit).")
    if force and was_reviewed:
        cfg = {**cfg, "rerun": True}  # the requester asked for it: the skill must not skip it as a duplicate
    ok, why = services.policy.acquire(user, "review")
    if not ok:
        state.release(key)
        return su.reply(channel, thread_ts, why)
    with state.lock:  # the reviews already running or waiting, before this one joins
        ahead = [j for k, j in state.jobs.items() if k != key and j.get("kind") == "review" and j.get("status") in ("queued", "running")]
    waiting = len(ahead) >= config.CONCURRENCY
    state.new_job(key, url=url, sha=sha, repo=repo, user=user, channel=channel, thread=thread_ts, waited=waiting)
    with state.lock:
        state.threads[(channel, thread_ts)] = key
    pool.submit(review, url, repo, number, sha, key, cfg, channel, thread_ts, msg_ts, user, info["is_fork"])
    services.policy.audit(user=user, capability="review", channel=channel, thread=thread_ts, pr=url, sha=sha[:7], decision="started")
    su.react(channel, msg_ts, "eyes")
    if waiting:
        running = next((j for j in ahead if j.get("status") == "running"), ahead[0])
        label = (running.get("url") or "another review").replace("https://github.com/", "").replace("/pull/", "#")
        su.reply(channel, thread_ts, f"<@{user}> Queued: {url} at `{sha[:7]}` is #{len(ahead) - config.CONCURRENCY + 1} in line, behind {label}"
                                     f"{' and ' + str(len(ahead) - 1) + ' more' if len(ahead) > 1 else ''}. "
                                     f"{'Reviews run one at a time' if config.CONCURRENCY == 1 else f'{config.CONCURRENCY} reviews run at a time'}. "
                                     "I'll tell you here the moment yours starts. Ask for status or tell me to stop at any time.")
    else:
        su.reply(channel, thread_ts, f"Review started on {url} at `{sha[:7]}`. Ask for status or tell me to stop at any time.")


def review(url, repo, number, sha, key, cfg, channel, thread_ts, msg_ts, user, is_fork):
    if key in state.cancelled:
        state.cancelled.discard(key)
        state.release(key)
        state.update_job(key, status="stopped", finished_at=time.time())
        state.finish_job(key)
        return su.reply(channel, thread_ts, "Cancelled before it started.")
    if (state.jobs.get(key) or {}).get("waited"):  # it sat in the queue: say so the moment it is picked up
        waited = int(time.time() - (state.jobs.get(key) or {}).get("queued_at", time.time()))
        su.reply(channel, thread_ts, f"<@{user}> Your turn: the review of {url} is running now (it waited {waited // 60}m {waited % 60:02d}s).")
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
    agents, skipped, skill_verdict = None, None, None
    state.update_job(key, status="running", started_at=time.time(), last_event=time.time(), activity="getting the PR ready")
    cost = 0.0
    try:
        subprocess.run(["git", "-C", clone, "fetch", "origin", f"pull/{number}/head"], check=True, capture_output=True)
        remove_worktree(clone, wt)  # a run that was killed leaves its folder behind; start from a clean one
        subprocess.run(["git", "-C", clone, "worktree", "add", "--detach", str(wt), sha], check=True, capture_output=True)
        env = claude_env.env_for(services.policy, "review", {"TMPDIR": f"{tmp}/"})  # trailing slash: fetch-pr.sh concatenates names onto it
        result = run_claude(
            ["claude", "-p", prompt(cfg["skill"], url, sha, post, tmp, bool(cfg.get("confirmed_big")), bool(cfg.get("rerun"))),
             "--output-format", "stream-json", "--verbose",
             "--permission-mode", "dontAsk", "--allowedTools", allowed, "--disallowedTools", denied]
            + (["--max-budget-usd", f"{max_usd:g}"] if max_usd else [])
            + ["--add-dir", str(tmp)],  # the run's own private temp dir (the fetch dir lives there) is readable and listable
            wt, env,
            raw, err, key=key, stop_key=(channel, thread_ts),
        )
        cost = result.get("total_cost_usd") or 0.0
        draft.write_text(result.get("result") or "")
        said = VERDICT_LINE.findall(result.get("result") or "")  # a skill that deliberately did not review (PR too big, closed, ...) ends with DEX_VERDICT: skip
        skill_verdict = said[-1].lower() if said else None
        if dex_posts:
            body = (result.get("result") or "").strip()
            if not body:
                raise RuntimeError("the review skill produced no text to post")
            verdict, body = split_verdict(body)
            skill_verdict = verdict
            if count_agent_calls(raw) == 0:  # nothing was reviewed by an agent: post nothing to GitHub, only tell the requester here
                verdict = "skip"
            note = ""
            flags = {"approve": "--approve", "request_changes": "--request-changes", "comment": "--comment"}
            body_file = tmp / "review-body.md"
            body_file.write_text(body[:60000] + f"\n\n---\n:robot: AI-assisted review (`{cfg['skill'].split()[0].lstrip('/')}`), posted by Dex with the skill's decision: "
                                 f"{verdict.replace('_', ' ')}.{note} A human can dismiss it.\n")

            def gh_review(flag):
                return subprocess.run(["gh", "pr", "review", number, "--repo", repo, flag, "--body-file", str(body_file)], capture_output=True, text=True)
            if verdict == "skip":
                skipped = body
            else:
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
            with open(config.DATA / "denials.jsonl", "a") as f:  # every denial, for adjusting the allowlist in one batch
                for d in denials:
                    f.write(json.dumps({"ts": datetime.datetime.now().isoformat(timespec="seconds"), "pr": url, "tool": d.get("tool_name"),
                                        "input": str((d.get("tool_input") or {}).get("command") or (d.get("tool_input") or {}))[:300]}) + "\n")
            serious = serious_denials(denials)
            if serious:
                su.dm(config.OWNER, f"Review of {url}: {len(serious)} risky tool call(s) were blocked (of {len(denials)} blocked in total):\n"
                      + "\n".join(shown(d) for d in serious[:8]))
        agents = count_agent_calls(raw)
        no_agents = agents == 0 and skill_verdict != "skip"  # a deliberate skip (draft, closed, ...) is not a shortcut
        usage = usage_line(key, cost, agents, started, profile, len(denials))
        if no_agents:
            log.warning("review of %s ran no review agents", key)
            su.dm(config.OWNER, f"Review of {url} ran NO review agents: the skill reviewed alone. Treat it as a quick read. "
                  f"The raw log is kept in {raw.name}.")
        link = ""
        if skipped is not None:
            outcome = "not reviewed (nothing posted)"
            su.reply(channel, thread_ts, f"<@{user}> No review was posted for {url}; nothing went to GitHub. Why:\n>{skipped[:1500].replace(chr(10), chr(10) + '>')}" + usage)
            done = "warning"
        elif post:
            found = github.posted_review(repo, number, started)
            if found:
                gh_state, link = found
                outcome = github.REVIEW_LABEL.get(gh_state, gh_state.lower())
                notes = ([":warning: The skill ran no review agents (it reviewed alone), so treat this as a quick read, not a full review."] if no_agents else []) \
                    + ([":information_source: The skill did not run a full review (for example the PR is too big); read its comment for why."] if skill_verdict == "skip" else [])
                su.reply(channel, thread_ts, done_message(user, url, outcome, link, notes, usage,
                                                          AGAIN_HINT if outcome in ("changes requested", "comments posted") and skill_verdict != "skip" else ""))
                done = "white_check_mark"
            elif skill_verdict == "skip":
                outcome = "not reviewed (nothing posted)"
                why = VERDICT_LINE.sub("", result.get("result") or "").strip()
                su.reply(channel, thread_ts, f"<@{user}> :information_source: No new review was posted on <{url}>. The skill stopped on purpose:\n>"
                         + why[:1400].replace("\n", "\n>") + usage)
                done = "information_source"
            else:
                outcome = "finished, but no posted review was found"
                su.reply(channel, thread_ts, f"<@{user}> Review finished but I could not find a posted review on <{url}>. Please check the PR." + usage)
                done = "warning"
        else:
            body = draft.read_text().strip()
            if not body:
                raise RuntimeError("review produced no output")
            outcome = "not posted (PR is from a fork)"
            su.reply(channel, thread_ts, f"<@{user}> Review done on {url}, not posted (PR is from a fork).\n\n{body[:2800]}" + usage)
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
        su.reply(channel, thread_ts, f"<@{user}> Stopped the review of {url}: it reached the ${max_usd:g} per-review limit (about ${cost:.2f} spent) before finishing, so nothing was posted. Ask the owner to raise the limit, or review a smaller PR." + usage_line(key, cost, agents, started, profile))
        su.dm(config.OWNER, f"The review of {url} was stopped at the ${max_usd:g} per-review limit (spent about ${cost:.2f}).")
        services.policy.audit(user=user, capability="review", channel=channel, thread=thread_ts, pr=url, decision="budget_stopped", usd=round(cost, 4), tokens=job_tokens(key))
    except UsageLimit as e:
        note_limit(profile, e)
        state.release(key)  # allow a retry on this SHA once the limit resets
        state.update_job(key, status="failed", outcome="usage limit reached", finished_at=time.time())
        su.react_remove(channel, msg_ts, "eyes")
        su.react(channel, msg_ts, "hourglass_flowing_sand")
        su.reply(channel, thread_ts, f"<@{user}> The review of {url} did not finish. " + limit_text(profile, e.reset, owner_hint=False) + " Send it again after the reset." + usage_line(key, cost, agents, started, profile))
        su.dm(config.OWNER, f"{limit_text(profile, e.reset)} It stopped the review of {url}. What Claude said: {e.text[:300]}")
        services.policy.audit(user=user, capability="review", channel=channel, thread=thread_ts, pr=url, decision="usage_limit", profile=profile, error=e.text[:200])
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
        su.reply(channel, thread_ts, f"<@{user}> Review of {url} failed." + usage_line(key, cost, agents, started, profile))
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
