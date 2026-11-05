"""Lane scheduler: which ready tasks to start now, and how wide a graph can run.

Pure and clock-free. Plain data in, plain data out, no I/O.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from core import workstore
from core.landing import DEFAULT_LAND_AT


def next_launches(
    items: Sequence[Mapping[str, Any]],
    in_flight: Collection[str],
    max_lanes: int,
    foreign: Mapping[str, str] | None = None,
    land_at: str = DEFAULT_LAND_AT,
) -> list[dict[str, Any]]:
    """Ready tasks to start now, by priority then id, at most the lanes still free.

    No phase filter: a task is eligible whatever its level, so a later-level task
    whose needs are met is not held by an unfinished earlier level. `land_at` decides
    whether an unmerged approved need counts as met.
    """
    free = max(0, max_lanes - len(in_flight))
    ready = workstore.ready_tasks(items, foreign=foreign, land_at=land_at)
    return [task for task in ready if str(task["id"]) not in in_flight][:free]


def resolve_max_parallel(cli_value: int | None, profile_value: int | None, ready_count: int) -> int:
    """First of the command-line value, the profile value, then the ready count, floored at 1."""
    first = next((v for v in (cli_value, profile_value) if v is not None), ready_count)
    return max(1, first)


def lane_bound(
    max_lanes: int, in_flight: int, headroom_usd: float | None, est_task_usd: float | None
) -> tuple[int, str]:
    """Tasks that may start now and the limit that set it. `in_flight` is a count, not ids.

    The usage limit applies only when both headroom and a positive per-task estimate are given.
    The binding is 'usage ceiling' only when that limit is strictly below the lane limit.
    """
    by_lanes = max(0, max_lanes - in_flight)
    by_usage = (
        max(0, math.floor(headroom_usd / est_task_usd))
        if headroom_usd is not None and est_task_usd is not None and est_task_usd > 0
        else None
    )
    if by_usage is not None and by_usage < by_lanes:
        return by_usage, "usage ceiling"
    return by_lanes, "lanes"


def _layers(needs: Mapping[str, tuple[str, ...]]) -> list[list[str]]:
    """Layer k holds the tasks whose longest chain of needs has k edges. Each layer is sorted by id.

    Raises ValueError naming the stuck ids when the needs contain a cycle.
    """
    layers: list[list[str]] = []
    placed: frozenset[str] = frozenset()
    while len(placed) < len(needs):
        layer = sorted(t for t in needs if t not in placed and all(n in placed for n in needs[t]))
        if not layer:
            raise ValueError(f"dependency cycle among: {sorted(set(needs) - placed)}")
        layers = [*layers, layer]
        placed = placed | frozenset(layer)
    return layers


def max_width(items: Sequence[Mapping[str, Any]]) -> tuple[int, list[list[str]]]:
    """Largest set of tasks with no needs path between any two, and the layers (each sorted by id).

    Only the needs edges count: state and phase are ignored, and a need outside `items` is ignored.
    The width is the true antichain size, which the largest layer can undercount.
    A cycle raises ValueError.
    """
    ids = {str(item["id"]) for item in items}
    needs = {str(item["id"]): tuple(str(n) for n in item.get("needs") or [] if str(n) in ids) for item in items}
    layers = _layers(needs)

    # Dilworth: width = tasks - maximum matching between each task and every task that transitively needs it.
    ancestors: dict[str, frozenset[str]] = {}
    for layer in layers:
        ancestors = {**ancestors, **{t: frozenset().union(*({n} | ancestors[n] for n in needs[t])) for t in layer}}
    later = {t: sorted(v for v in needs if t in ancestors[v]) for t in needs}

    matched_to: dict[str, str] = {}

    def augment(task: str, seen: set[str]) -> bool:
        for nxt in later[task]:
            if nxt in seen:
                continue
            seen.add(nxt)
            if nxt not in matched_to or augment(matched_to[nxt], seen):
                matched_to[nxt] = task
                return True
        return False

    matches = sum(augment(t, set()) for t in sorted(needs))
    return len(needs) - matches, layers
