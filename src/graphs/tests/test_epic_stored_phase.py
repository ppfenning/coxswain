"""A phase is finished by its stored task state, not by what this run happened to build."""

from __future__ import annotations

import pytest

from harness.epic import _phase_status, open_in_phase

MERGED = {"id": "m", "phase": "p1", "state": "approved", "merged": True}
UNMERGED = {"id": "u", "phase": "p1", "state": "approved", "merged": False}
QUARANTINED = {"id": "q", "phase": "p1", "state": "quarantined"}


@pytest.mark.parametrize(
    ("land_at", "expected"),
    [("ticket", []), ("level", []), ("task", ["u"])],
)
def test_an_approved_item_from_an_earlier_run_is_open_only_when_unmerged_at_task(
    land_at: str, expected: list[str]
) -> None:
    assert open_in_phase([MERGED, UNMERGED], "p1", (), land_at) == expected


def test_done_and_dropped_items_are_finished_and_other_states_are_open() -> None:
    items = [
        {"id": "a", "phase": "p1", "state": "done"},
        {"id": "b", "phase": "p1", "state": "dropped"},
        {"id": "c", "phase": "p1", "state": "ready"},
        QUARANTINED,
        {"id": "z", "phase": "p2", "state": "ready"},
    ]
    assert open_in_phase(items, "p1", (), "task") == ["c", "q"]


def _status(items: list[dict], land_at: str = "ticket") -> tuple[str, str]:
    return _phase_status(
        {"goal_met": True},
        validated=True,
        ready=[],
        quarantined=[],
        items=items,
        phase="p1",
        landed=True,
        branch="phase/p1",
        land_at=land_at,
    )


def test_a_stored_quarantined_item_keeps_the_phase_partial_despite_a_met_goal() -> None:
    assert _status([MERGED, QUARANTINED]) == ("partial", "a task in the phase is quarantined")


def test_stored_approved_items_complete_the_phase_by_the_same_rule() -> None:
    assert _status([UNMERGED, {"id": "d", "phase": "p1", "state": "dropped"}])[0] == "complete"
