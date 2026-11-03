"""Lane scheduler on literals."""

from __future__ import annotations

import pytest

from harness.lanes import max_width, next_launches, resolve_max_parallel


def _task(tid: str, needs: list[str] | None = None, state: str = "ready", priority: int = 3, phase: str = "p1") -> dict:
    return {"id": tid, "needs": needs or [], "state": state, "priority": priority, "phase": phase}


def _done(items: list[dict], *ids: str) -> list[dict]:
    return [{**i, "state": "done"} if i["id"] in ids else i for i in items]


def _ids(tasks: list[dict]) -> list[str]:
    return [t["id"] for t in tasks]


def test_three_independent_and_four_dependents_fill_three_lanes_then_refill() -> None:
    base = [_task(x) for x in ("a", "b", "c")] + [_task(f"d{n}", ["a", "b", "c"], phase="p2") for n in range(4)]
    first = next_launches(base, set(), 3)
    assert _ids(first) == ["a", "b", "c"]
    after = _done(base, "a", "b", "c")
    second = next_launches(after, set(), 3)
    assert _ids(second) == ["d0", "d1", "d2"]
    third = next_launches(_done(after, "d0", "d1", "d2"), set(), 3)
    assert _ids(third) == ["d3"]


def test_task_needing_two_earlier_levels_does_not_wait_for_an_unrelated_level() -> None:
    items = [
        _task("l1a", state="done"),
        _task("l1slow", state="in_progress"),
        _task("l2", ["l1a"], state="done", phase="p2"),
        _task("l3", ["l1a", "l2"], phase="p3"),
    ]
    assert _ids(next_launches(items, {"l1slow"}, 3)) == ["l3"]


def test_cap_bounds_launches_and_in_flight_is_excluded() -> None:
    items = [_task(x) for x in ("a", "b", "c", "d")]
    assert _ids(next_launches(items, set(), 2)) == ["a", "b"]
    assert _ids(next_launches(items, {"a"}, 2)) == ["b"]
    assert next_launches(items, {"a", "b"}, 2) == []
    assert next_launches(items, {"x", "y", "z"}, 2) == []


def test_launches_are_ordered_by_priority_then_id() -> None:
    items = [_task("a", priority=3), _task("c", priority=1), _task("b", priority=1)]
    assert _ids(next_launches(items, set(), 3)) == ["b", "c", "a"]


def test_unfinished_earlier_phase_does_not_hold_a_later_phase_task() -> None:
    items = [_task("early", phase="p1"), _task("late", phase="p2")]
    assert _ids(next_launches(items, set(), 2)) == ["early", "late"]
    assert _ids(next_launches(items, set(), 1)) == ["early"]


def test_foreign_done_need_is_met() -> None:
    items = [_task("a", ["other-1"])]
    assert next_launches(items, set(), 1) == []
    assert _ids(next_launches(items, set(), 1, {"other-1": "done"})) == ["a"]


def test_max_parallel_precedence_and_floor() -> None:
    assert resolve_max_parallel(4, 2, 9) == 4
    assert resolve_max_parallel(None, 2, 9) == 2
    assert resolve_max_parallel(None, None, 9) == 9
    assert resolve_max_parallel(None, None, 0) == 1
    assert resolve_max_parallel(0, 2, 9) == 1


def test_width_of_a_chain_is_one() -> None:
    chain = [_task("a"), _task("b", ["a"]), _task("c", ["b"]), _task("d", ["c"])]
    assert max_width(chain) == (1, [["a"], ["b"], ["c"], ["d"]])


def test_width_of_a_wide_graph() -> None:
    items = [
        _task("root"),
        _task("x", ["root"]),
        _task("y", ["root"]),
        _task("z", ["root"]),
        _task("join", ["x", "y", "z"]),
        _task("late", ["root", "join"]),
    ]
    assert max_width(items) == (3, [["root"], ["x", "y", "z"], ["join"], ["late"]])


def test_width_counts_tasks_that_run_together_not_the_largest_layer() -> None:
    items = [
        _task("r"),
        _task("p1", ["r"]),
        _task("q1", ["r"]),
        _task("p2", ["p1"]),
        _task("u"),
    ]
    assert max_width(items) == (3, [["r", "u"], ["p1", "q1"], ["p2"]])


def test_width_ignores_state_phase_and_needs_outside_the_set() -> None:
    items = [_task("a", ["gone"], state="done"), _task("b", phase="p9", state="todo")]
    assert max_width(items) == (2, [["a", "b"]])


def test_width_of_a_cycle_is_refused() -> None:
    with pytest.raises(ValueError, match="cycle"):
        max_width([_task("a", ["b"]), _task("b", ["a"]), _task("c")])


def test_width_of_nothing_is_zero() -> None:
    assert max_width([]) == (0, [])
