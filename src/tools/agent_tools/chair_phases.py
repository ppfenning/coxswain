"""Which phases of one initiative can start a lane now, and with which ready tasks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from agent_tools import route


def _phase_parents(items: Sequence[Mapping]) -> dict[str, set[str]]:
    """Phase B has parent A when a task in B needs a task in A that is not done; a done need adds none.

    Mirrors `phase_parents` in the graphs harness, which this package does not import.
    """
    phase_of = {str(item["id"]): str(item.get("phase") or "") for item in items}
    state_of = {str(item["id"]): item.get("state") for item in items}
    edges = [
        (str(item.get("phase") or ""), phase_of.get(str(need)))
        for item in items
        for need in item.get("needs") or []
        if state_of.get(str(need)) != "done"
    ]
    return {
        phase: {there for here, there in edges if here == phase and there and there != phase}
        for phase in phase_of.values()
        if phase
    }


def launchable_phases(items: Sequence[Mapping], land_at: str) -> dict[str, list[str]]:
    """Phase id to its ready task ids, for phases whose parent tasks are all met under `land_at`.

    `items` are the rows of one initiative (`id`, `phase`, `state`, `merged`, `needs`). A ready task counts only when
    its own needs are met; a need naming no row is unmet. Phases keep their first-appearance order.
    """
    by_id = {str(item["id"]): item for item in items}
    parents = _phase_parents(items)

    def met(item: Mapping) -> bool:
        return route.is_met_need(item, land_at=land_at)

    def parents_met(phase: str) -> bool:
        return all(met(item) for item in items if str(item.get("phase") or "") in parents[phase])

    def needs_met(item: Mapping) -> bool:
        return all(str(need) in by_id and met(by_id[str(need)]) for need in item.get("needs") or [])

    launchable = {
        phase: [
            str(item["id"])
            for item in items
            if str(item.get("phase") or "") == phase and item.get("state") == "ready" and needs_met(item)
        ]
        for phase in parents
        if parents_met(phase)
    }
    return {phase: ids for phase, ids in launchable.items() if ids}
