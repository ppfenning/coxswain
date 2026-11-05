"""A phase-two task needing an unmerged approved phase-one task launches at ticket and level, not at task."""

from __future__ import annotations

import pytest

from harness.lanes import next_launches
from harness.scheduler import plan_launches

UNMERGED = [
    {"id": "a", "needs": [], "state": "approved", "merged": False, "priority": 3, "phase": "one"},
    {"id": "b", "needs": ["a"], "state": "ready", "priority": 3, "phase": "two"},
]
MERGED = [{**UNMERGED[0], "merged": True}, UNMERGED[1]]


def _ids(tasks: list[dict]) -> list[str]:
    return [t["id"] for t in tasks]


@pytest.mark.parametrize("land_at", ["ticket", "level"])
def test_unmerged_approved_need_is_met_at_ticket_and_level(land_at: str) -> None:
    assert _ids(next_launches(UNMERGED, set(), 3, land_at=land_at)) == ["b"]


def test_unmerged_approved_need_holds_phase_two_at_task() -> None:
    assert next_launches(UNMERGED, set(), 3, land_at="task") == []


def test_merged_approved_need_is_met_at_task() -> None:
    assert _ids(next_launches(MERGED, set(), 3, land_at="task")) == ["b"]


def test_plan_launches_forwards_land_at() -> None:
    assert plan_launches(UNMERGED, {}, 3, land_at="ticket").start == {"two": ("b",)}
    assert plan_launches(UNMERGED, {}, 3, land_at="task").start == {}
