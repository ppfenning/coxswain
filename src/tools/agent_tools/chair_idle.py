"""Idle decision for the self-sustaining chair: is there anything to do, and how long may the loop wait."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from agent_tools.chair_plan_land import plan_lands
from agent_tools.chair_types import Facts

__all__ = ["Idle", "is_idle", "next_wait"]

FALLBACK_WAIT_S = 600.0


@dataclass(frozen=True)
class Idle:
    idle: bool
    reason: str


def _ready_intake(facts: Facts) -> list[str]:
    """`facts["intake"]` less the paths at the empty-decompose limit, which stay listed but `chair_plan_fill` never launches."""
    stalled = {path for path, streak in facts.get("decompose_streaks", {}).items() if streak.run_ids}
    return [path for path in facts["intake"] if path not in stalled]


def is_idle(facts: Facts, pending_directives: int = 0, now: datetime | None = None) -> Idle:
    """Not idle while a directive is pending, a task is ready, `plan_lands` has an action, or intake is ready.

    A phase held for review, a manual merge, a ticket or parent, a backoff, a standing conflict or a freeze plans no land.
    """
    ready = sorted(i["id"] for i in facts["initiatives"] if i["ready_tasks"])
    landable = sorted({str(a["kind"]) for a in plan_lands(facts, now)})
    intake = _ready_intake(facts)
    if pending_directives > 0:
        return Idle(False, f"{pending_directives} pending directive(s)")
    if ready:
        return Idle(False, f"ready task in {', '.join(ready)}")
    if landable:
        return Idle(False, f"land planned: {', '.join(landable)}")
    if intake:
        return Idle(False, f"{len(intake)} queued intake")
    return Idle(True, "")


def next_wait(idle: bool, event_interval_s: float, fallback_s: float = FALLBACK_WAIT_S) -> tuple[float, float]:
    """(poll step, ceiling) in seconds. Idle waits up to `fallback_s`; otherwise the normal interval."""
    ceiling = fallback_s if idle else event_interval_s
    return min(event_interval_s, ceiling), ceiling
