"""Launch planner: which tasks start now, and why the rest wait.

Pure and clock-free. Plain data in, plain data out, no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from harness.lanes import lane_bound, next_launches
from harness.overlap import serialise

_WAITING = frozenset({"todo", "ready", "blocked"})


@dataclass(frozen=True)
class Plan:
    """`start` maps phase to sorted task ids to start now; `held` maps task id to the reason it waits."""

    start: dict[str, tuple[str, ...]]
    held: dict[str, str]


def _surfaces(item: Mapping[str, Any]) -> list[str]:
    return [str(s) for s in item.get("surfaces") or []]


def _quarantined_holds(items: Sequence[Mapping[str, Any]], foreign: Mapping[str, str]) -> dict[str, str]:
    """A waiting task is held for each need in state quarantined, here or in another initiative."""
    states = {**foreign, **{str(i["id"]): str(i.get("state")) for i in items}}
    return {
        str(i["id"]): f"need '{need}' is quarantined"
        for i in items
        if i.get("state") in _WAITING
        for need in [next((str(n) for n in i.get("needs") or [] if states.get(str(n)) == "quarantined"), None)]
        if need is not None
    }


def plan_launches(
    items: Sequence[Mapping[str, Any]],
    running: Mapping[str, frozenset[str]],
    max_lanes: int,
    foreign: Mapping[str, str] | None = None,
    headroom_usd: float | None = None,
    est_task_usd: float = 0.0,
) -> Plan:
    """Tasks to start now by phase, and a reason for every ready task that waits.

    A phase in `running` has one build call in flight and gets no second one.
    """
    in_flight = frozenset().union(*running.values())
    slots, binding = lane_bound(max_lanes, len(in_flight), headroom_usd, est_task_usd)

    # next_launches cuts to max_lanes minus in-flight; ask for every candidate so each cut task gets a reason below.
    candidates = next_launches(items, in_flight, len(items) + len(in_flight), foreign)

    by_id = {str(i["id"]): i for i in items}
    busy = {
        str(t["id"]): f"phase '{t['phase']}' has a build call in flight"
        for t in candidates
        if str(t["phase"]) in running
    }

    phases = sorted({str(t["phase"]) for t in candidates} - set(running))
    by_phase = {p: [t for t in candidates if str(t["phase"]) == p] for p in phases}
    surfaces = {p: sorted({s for t in tasks for s in _surfaces(t)}) for p, tasks in by_phase.items()}
    running_surfaces = {
        p: sorted({s for tid in ids if tid in by_id for s in _surfaces(by_id[tid])}) for p, ids in running.items()
    }
    chosen, phase_held = serialise(phases, surfaces, running_surfaces)
    overlap = {str(t["id"]): phase_held[p] for p in phase_held for t in by_phase[p]}

    ordered = [t for t in candidates if str(t["phase"]) in chosen]
    kept, cut = ordered[:slots], ordered[slots:]
    over_bound = {str(t["id"]): f"held by {binding}: {slots} free slot(s)" for t in cut}
    start = {
        p: tuple(sorted(str(t["id"]) for t in kept if str(t["phase"]) == p))
        for p in chosen
        if any(str(t["phase"]) == p for t in kept)
    }
    return Plan(
        start=start,
        held={**_quarantined_holds(items, foreign or {}), **busy, **overlap, **over_bound},
    )
