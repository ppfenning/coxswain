"""Phase waves are computed from task needs: one literal case per rule."""

from __future__ import annotations

from harness.waves import compute_waves, phase_parents, widest_wave


def task(id: str, phase: str, needs: list[str] | None = None, state: str = "planned") -> dict:
    return {"id": id, "phase": phase, "needs": needs or [], "state": state}


def test_two_independent_chains_give_two_and_two() -> None:
    items = [task("a1", "a1p"), task("a2", "a2p", ["a1"]), task("b1", "b1p"), task("b2", "b2p", ["b1"])]
    waves, cyclic = compute_waves(phase_parents(items))
    assert waves == [["a1p", "b1p"], ["a2p", "b2p"]]
    assert cyclic == []
    assert widest_wave(waves) == 2


def test_diamond_gives_one_two_one() -> None:
    items = [
        task("t1", "top"),
        task("t2", "left", ["t1"]),
        task("t3", "right", ["t1"]),
        task("t4", "join", ["t2", "t3"]),
    ]
    waves, cyclic = compute_waves(phase_parents(items))
    assert waves == [["top"], ["left", "right"], ["join"]]
    assert cyclic == []


def test_done_need_adds_no_parent() -> None:
    items = [task("t1", "p1", state="done"), task("t2", "p2", ["t1"])]
    assert phase_parents(items) == {"p1": set(), "p2": set()}


def test_open_need_adds_parent_and_same_phase_need_does_not() -> None:
    items = [task("t1", "p1"), task("t2", "p1", ["t1"]), task("t3", "p2", ["t1"])]
    assert phase_parents(items) == {"p1": set(), "p2": {"p1"}}


def test_task_without_phase_is_ignored() -> None:
    items = [task("t1", ""), task("t2", "p1", ["t1"])]
    assert phase_parents(items) == {"p1": set()}


def test_cycle_is_cyclic_and_kept_out_of_waves() -> None:
    items = [task("t0", "root"), task("t1", "x", ["t2"]), task("t2", "y", ["t1"]), task("t3", "z", ["t1"])]
    waves, cyclic = compute_waves(phase_parents(items))
    assert waves == [["root"]]
    assert cyclic == ["x", "y", "z"]


def test_empty_input_gives_no_waves_and_width_zero() -> None:
    waves, cyclic = compute_waves(phase_parents([]))
    assert (waves, cyclic) == ([], [])
    assert widest_wave(waves) == 0
