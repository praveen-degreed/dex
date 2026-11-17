"""When a Claude account reaches its usage limit: remember it, say so clearly, and do not start new work on it until it resets."""
import datetime
import json
import time

from . import claude_env, config, state

LIMIT_TTL_S = 30 * 60  # without a reset time in the message, believe a hit limit for this long before trying again


def account_name(profile):
    return "Azure Foundry" if profile == claude_env.FOUNDRY else f"the {profile} Claude plan"


def limit_text(profile, reset=None, owner_hint=True):
    when = f" It resets around {reset:%H:%M}." if reset else ""
    hint = (f" The owner can switch accounts: `dexctl {'foundry' if profile != claude_env.FOUNDRY else 'subscription'}`." if owner_hint else "")
    return f":hourglass_flowing_sand: {account_name(profile)} has reached its usage limit.{when}{hint}"


def limit_active(profile):
    """The (time, reset, text) of a recent limit hit on this account that has not reset yet, else None."""
    hit = state.limit_hits.get(profile)
    if not hit:
        return None
    t, reset, _ = hit
    if (reset and datetime.datetime.now() < reset) or (not reset and time.time() - t < LIMIT_TTL_S):
        return hit
    state.limit_hits.pop(profile, None)
    return None


def note_limit(profile, e):
    state.limit_hits[profile] = (time.time(), e.reset, e.text)
    with open(config.DATA / "limits.jsonl", "a") as f:  # the raw wording, to tune the detection against the real messages
        f.write(json.dumps({"ts": datetime.datetime.now().isoformat(timespec="seconds"), "profile": profile, "text": e.text}) + "\n")
