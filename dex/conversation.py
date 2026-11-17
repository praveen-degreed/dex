"""The assistant flow: a Slack thread is one Claude session with read access to every configured repo, its git
history and GitHub (see qa.py). The assistant asks a clarifying question itself when it needs one; the person's
reply lands in the same thread and resumes the same session."""
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import claude_env, config, people, qa, services, state
from . import slack_utils as su
from .claude_runner import BudgetExceeded, Stopped, run_claude

log = logging.getLogger("dex")
qa_pool = ThreadPoolExecutor(max_workers=config.QA_CONCURRENCY)
_locks, _locks_guard = {}, threading.Lock()


def _thread_lock(k):
    with _locks_guard:
        return _locks.setdefault(k, threading.Lock())


def asker_line(user):
    """Tells the assistant who is asking, so "my open PRs" needs no question back."""
    p = people.get(user)
    if p and p.get("github"):
        return f"[From: {p.get('name', user)} (GitHub `{p['github']}`)]"
    return f"[From: {(p or {}).get('name') or 'an unknown person'} (Slack <@{user}>; no GitHub login on file)]"


def _tokens(key):
    with state.lock:
        return dict((state.jobs.get(key) or {}).get("tokens") or {})


def start_question(channel, thread_ts, msg_ts, user, text, verdict, review_key, session, parent_text):
    """Count the request and queue the answer. There is no repo gate: the assistant sees every repo."""
    if session and session.get("turns", 0) >= config.MAX_QA_TURNS:
        return su.reply(channel, thread_ts, f"This thread has reached {config.MAX_QA_TURNS} questions. "
                                            "Start a new thread to keep going.")
    ok, why = services.policy.acquire(user, "question")
    if not ok:
        return su.reply(channel, thread_ts, why)
    header = ""
    with state.lock:
        job = state.jobs.get(review_key) if review_key else None
    if job:
        header = (f"[Context: this Slack thread is about the review of {job['url']} at commit {job['sha'][:7]}. "
                  "The code you can read is main; use `gh pr view` / `gh pr diff` for that PR's own changes.]\n")
    elif parent_text and not session:
        header = f"[Context: this Slack thread started with: {su.render_mentions(parent_text, config.OWNER)[:500]}]\n"
    su.react(channel, msg_ts, "eyes")
    qa_pool.submit(answer_question, channel, thread_ts, msg_ts, user, f"{asker_line(user)}\n{su.render_mentions(text, config.OWNER)}", header)


def answer_question(channel, thread_ts, msg_ts, user, text, header):
    k = (channel, thread_ts)
    with _thread_lock(k):
        name = f"qa-{channel}-{thread_ts}".replace(".", "_")
        raw, err = config.TMPS / f"{name}.jsonl", config.TMPS / f"{name}.err"
        with state.lock:
            state.qa_active[k] = time.time()
        qkey = f"q:{channel}:{thread_ts}:{time.time_ns()}"
        state.new_job(qkey, kind="question", label=f"A question from <@{user}>", url="", sha="", repo="", user=user, channel=channel, thread=thread_ts)
        state.update_job(qkey, status="running", started_at=time.time(), last_event=time.time(), activity="working on it")
        max_usd = services.policy.max_job_usd("question")
        cost, final = 0.0, "failed"
        try:
            sess = services.sessions.get(channel, thread_ts)
            sid = (sess or {}).get("session_id")
            for attempt in (1, 2):
                prompt = (header + text) if (header and not sid) else text
                try:
                    result = run_claude(claude_env.adapt_command(services.policy, "question", qa.build_command(prompt, sid, max_usd)),
                                        config.QA_DIR, claude_env.env_for(services.policy, "question"), raw, err,
                                        key=qkey, timeout=config.QA_TIMEOUT_S, stop_key=k, label="answer")
                    break
                except RuntimeError as e:
                    if sid and attempt == 1 and "No conversation found" in str(e):
                        sid = None  # the saved session is gone: start this thread's conversation again
                        continue
                    raise
            cost = result.get("total_cost_usd") or 0.0
            answer = su.to_slack(str(result.get("result") or "")) or "I couldn't produce an answer to that."
            prev = sess or {"turns": 0, "cost": 0.0}
            services.sessions.put(channel, thread_ts, session_id=result.get("session_id") or sid, user=user,
                                  turns=prev["turns"] + 1, cost=prev["cost"] + cost)
            su.reply(channel, thread_ts, answer)
            final = "done"
            services.policy.audit(user=user, capability="question", channel=channel, thread=thread_ts,
                                  decision="answered", usd=round(cost, 4), turns=prev["turns"] + 1,
                                  denials=len(result.get("permission_denials") or []), tokens=_tokens(qkey))
        except BudgetExceeded as e:
            cost, final = e.cost, "stopped"
            su.reply(channel, thread_ts, f"I stopped that answer at the ${max_usd:g} per-answer limit (about ${e.cost:.2f} spent). Try a narrower question, or ask the owner to raise the limit.")
            services.policy.audit(user=user, capability="question", channel=channel, thread=thread_ts, decision="budget_stopped", usd=round(cost, 4), tokens=_tokens(qkey))
        except Stopped:
            final = "stopped"
            su.reply(channel, thread_ts, "Stopped.")
            services.policy.audit(user=user, capability="question", channel=channel, thread=thread_ts, decision="stopped")
        except Exception as e:
            log.warning("question failed: %s", str(e)[:300])
            su.reply(channel, thread_ts, "I couldn't answer that just now. Try again in a moment.")
            su.dm(config.OWNER, f"A question in a thread failed: {str(e)[:400]}")
            services.policy.audit(user=user, capability="question", channel=channel, thread=thread_ts, decision="failed",
                                  error=str(e)[:200])
        finally:
            services.policy.add_cost(user, cost, _tokens(qkey))
            state.update_job(qkey, status=final, outcome=final, finished_at=time.time())
            state.finish_job(qkey)
            with state.lock:
                state.qa_active.pop(k, None)
            su.react_remove(channel, msg_ts, "eyes")
            raw.unlink(missing_ok=True)
            err.unlink(missing_ok=True)
