from __future__ import annotations

from typing import Any

from harness.scheduler import plan_launches


def _item(
    id: str, phase: str, state: str = "ready", needs: tuple[str, ...] = (), surfaces: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {"id": id, "phase": phase, "state": state, "needs": list(needs), "surfaces": list(surfaces)}


def test_two_independent_chains_start_one_task_each() -> None:
    items = [
        _item("a1", "A"),
        _item("a2", "A", "todo", ("a1",)),
        _item("b1", "B"),
        _item("b2", "B", "todo", ("b1",)),
    ]
    plan = plan_launches(items, {}, 4)
    assert plan.start == {"A": ("a1",), "B": ("b1",)}
    assert plan.held == {}


def test_level_3_task_starts_while_unrelated_level_1_task_runs() -> None:
    items = [
        _item("x1", "P1", "in_progress"),
        _item("y1", "P1", "done"),
        _item("y2", "P2", "done", ("y1",)),
        _item("y3", "P3", "ready", ("y2",)),
    ]
    plan = plan_launches(items, {"P1": frozenset({"x1"})}, 3)
    assert plan.start == {"P3": ("y3",)}


def test_quarantined_need_holds_only_its_own_dependents() -> None:
    items = [
        _item("q1", "P1", "quarantined"),
        _item("d1", "P2", "ready", ("q1",)),
        _item("d2", "P2", "todo", ("d1",)),
        _item("e1", "P3"),
    ]
    plan = plan_launches(items, {}, 3)
    assert plan.start == {"P3": ("e1",)}
    assert plan.held == {"d1": "need 'q1' is quarantined"}


def test_phases_sharing_a_surface_start_one_and_hold_the_other() -> None:
    items = [_item("a1", "A", surfaces=("src/x.py",)), _item("b1", "B", surfaces=("src/x.py",))]
    plan = plan_launches(items, {}, 3)
    assert plan.start == {"A": ("a1",)}
    assert "waits for phase 'A'" in plan.held["b1"]


def test_phase_in_running_gets_nothing_new() -> None:
    items = [_item("a1", "A", "in_progress"), _item("a2", "A")]
    plan = plan_launches(items, {"A": frozenset({"a1"})}, 3)
    assert plan.start == {}
    assert "in flight" in plan.held["a2"]


def test_usage_limited_bound_holds_the_overflow() -> None:
    items = [_item("a1", "A"), _item("b1", "B"), _item("c1", "C")]
    plan = plan_launches(items, {}, 5, headroom_usd=10.0, est_task_usd=5.0)
    assert plan.start == {"A": ("a1",), "B": ("b1",)}
    assert "usage ceiling" in plan.held["c1"]


def test_lane_bound_holds_the_overflow_as_lanes() -> None:
    plan = plan_launches([_item("a1", "A"), _item("b1", "B")], {}, 1)
    assert plan.start == {"A": ("a1",)}
    assert "lanes" in plan.held["b1"]
