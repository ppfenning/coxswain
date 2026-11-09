"""Idle with ready work: one needs_chair that names the steer cycle, sent once per set of initiatives.

The steer_deferred needs_chair lines never push, so a cycle of initiatives each waiting on another's run stays silent.
After IDLE_READY_MINUTES with no live lane, ready work and no launch, this rule names the cycle in one pushing action.
Its signature carries no timestamp, so while `open_signature` equals it the rule returns nothing.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime

from agent_tools.chair_types import Action

IDLE_READY_MINUTES = 30
IDLE_READY_CAUSE = "idle_ready"
_LAUNCH_KINDS = frozenset({"launch_epic", "relaunch", "retry", "rescue"})


def _no_live_run(facts: Mapping) -> bool:
    dispatch = facts.get("dispatch") or {}
    return dispatch.get("live_runs") == 0 and all(h.get("live_runs") == 0 for h in dispatch.get("hosts") or [])


def _idle_minutes(facts: Mapping, now: datetime) -> float | None:
    """Minutes from `last_progress_at` to `now`; None when the stamp is missing or unreadable."""
    stamp = (facts.get("idle_stall") or {}).get("last_progress_at")
    try:
        return (now - datetime.fromisoformat(stamp)).total_seconds() / 60 if stamp else None
    except (TypeError, ValueError):
        return None


def _waits(actions: Sequence[Mapping]) -> dict[str, list[str]]:
    """Initiative to the sorted initiatives it waits on, from the tick's steer_deferred needs_chair actions."""
    pairs = {
        (str(a["initiative"]), str(a["other"]))
        for a in actions
        if a.get("kind") == "needs_chair" and a.get("cause") == "steer_deferred" and a.get("initiative") and a.get("other")
    }
    return {i: sorted(o for j, o in pairs if j == i) for i in sorted({i for i, _ in pairs})}


def _reason(ids: list[str], waits: Mapping[str, list[str]]) -> str:
    lines = [f"{i} waits on {', '.join(waits[i])}" if i in waits else f"{i} has ready work and is not waiting on a run" for i in ids]
    return f"Idle {IDLE_READY_MINUTES} minutes with ready work and no launch. " + "; ".join(lines) + "."


def plan_idle_ready(facts: Mapping, actions: Sequence[Mapping], now: datetime) -> list[Action]:
    """One needs_chair with cause idle_ready, or nothing. A missing `last_progress_at` counts as not yet idle."""
    ready = sorted(str(i["id"]) for i in facts.get("initiatives") or [] if i.get("ready_tasks"))
    idle = _idle_minutes(facts, now)
    if not ready or idle is None or idle < IDLE_READY_MINUTES:
        return []
    if not _no_live_run(facts) or any(a.get("kind") in _LAUNCH_KINDS for a in actions):
        return []
    waits = _waits(actions)
    ids = sorted(set(waits) | {o for os in waits.values() for o in os}) or ready
    signature = "idle_ready:" + ",".join(ids)
    if (facts.get("idle_stall") or {}).get("open_signature") == signature:
        return []
    return [{
        "kind": "needs_chair",
        "initiative": ids[0],
        "cause": IDLE_READY_CAUSE,
        "signature": signature,
        "reason": _reason(ids, waits),
    }]
