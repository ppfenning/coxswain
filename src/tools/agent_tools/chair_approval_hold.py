"""Release an approval that has sat unmerged for the hold limit, so it stops holding files.

Pure rules over the tick's facts: `plan_release` names the approvals to release and `without_released`
drops what they held from the running entries. Nothing here reads a file, a clock or the store.
"""

from collections.abc import Mapping
from typing import cast

from agent_tools.chair_types import Action, Facts, RunningInitiative

DEFAULT_HOLD_TICKS = 6


def _limit(facts: Facts) -> int:
    return facts.get("approval_hold_ticks", DEFAULT_HOLD_TICKS)


def plan_release(facts: Facts) -> list[Action]:
    """One release_approvals action per initiative with a hold at or above the limit; a merged task is never released."""
    limit = _limit(facts)
    merged = {(t["initiative"], t["id"]) for t in facts.get("approved", []) if t["merged"]}
    stuck = [
        h for h in facts.get("approval_holds", [])
        if h["ticks"] >= limit and (h["initiative"], h["task_id"]) not in merged
    ]
    return [
        {
            "kind": "release_approvals",
            "initiative": initiative,
            "task_ids": [h["task_id"] for h in stuck if h["initiative"] == initiative],
            "reason": f"approval unmerged for {limit} ticks; rebuild on the current main",
        }
        for initiative in dict.fromkeys(h["initiative"] for h in stuck)
    ]


def _surfaces(hold: object) -> set[str]:
    # A hold carries `surfaces` only when its producer filled it; a hold without it frees nothing.
    return set(cast(Mapping[str, list[str]], hold).get("surfaces", []))


def without_released(facts: Facts, actions: list[Action]) -> Facts:
    """Facts with each released initiative's non-live running entry stripped of the released tasks' surfaces.

    An entry whose every hold is released is removed; a live entry is untouched. The input is not mutated."""
    released = {a["initiative"]: set(a.get("task_ids", [])) for a in actions if a["kind"] == "release_approvals"}
    holds = facts.get("approval_holds", [])

    def strip(entry: RunningInitiative) -> RunningInitiative | None:
        gone = released.get(entry["id"])
        mine = [h for h in holds if h["initiative"] == entry["id"]]
        if gone is None or entry.get("live", True) or not mine:
            return entry
        kept = [h for h in mine if h["task_id"] not in gone]
        if not kept:
            return None
        freed = {s for h in mine if h["task_id"] in gone for s in _surfaces(h)} - {s for h in kept for s in _surfaces(h)}
        return {**entry, "surfaces": [s for s in entry["surfaces"] if s not in freed]}

    if "running" not in facts:
        return {**facts}
    return {**facts, "running": [e for e in map(strip, facts["running"]) if e is not None]}
