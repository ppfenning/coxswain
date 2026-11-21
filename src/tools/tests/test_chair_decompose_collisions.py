"""C5: a `collision` step row (L5's local merge agent, `lane_merge.collision_row`) counts toward the decompose-miss
rules: two tasks the decompose called independent edited the same files. The evidence is per initiative and per run:
each collision goes on that initiative's decompose run launched last before it, so a later, honest decompose starts
clean. The store read is the harness's own over a real sqlite store; the rest is pure. No mocks."""

from datetime import UTC, datetime
from pathlib import Path

from agent_tools import chair_decompose_runs, run_store
from agent_tools.chair_decompose_streak import DecomposeRun, at_limit, stalled_reason, streak, with_collisions
from harness import store_steps
from harness.store_migrate import open_store


def _run(run_id: str, hour: int, wrote: bool = True) -> DecomposeRun:
    return DecomposeRun(run_id, wrote, None, datetime(2026, 11, 21, hour, tzinfo=UTC))


def _collision(initiative: str, at: str, task: str = "t2", other: str = "t1", files: tuple[str, ...] = ("lib/a.py",)) -> dict:
    return {"initiative": initiative, "phase": "p1", "task_id": task, "finished_at": at,
            "result_json": {"task": task, "other": other, "files": list(files)}}


def test_a_collision_goes_on_the_run_launched_last_before_it():
    runs = [_run("ship-1", 8), _run("ship-2", 10)]
    out = with_collisions(runs, [_collision("ship", "2026-11-21T09:00:00Z"), _collision("ship", "2026-11-21T11:00:00Z", "t3")])
    assert [r.collisions for r in out] == [("p1/t2~t1: lib/a.py",), ("p1/t3~t1: lib/a.py",)]


def test_a_collision_with_no_time_or_before_every_run_goes_on_the_newest_run():
    runs = [_run("ship-1", 8), _run("ship-2", 10)]
    out = with_collisions(runs, [_collision("ship", ""), _collision("ship", "2026-11-20T00:00:00Z")])
    assert [len(r.collisions) for r in out] == [0, 2]
    assert with_collisions([], [_collision("ship", "")]) == []


def test_two_decomposes_in_a_row_whose_tasks_collided_reach_the_limit_though_both_wrote_tasks():
    runs = with_collisions([_run("ship-1", 8), _run("ship-2", 10)],
                           [_collision("ship", "2026-11-21T09:00:00Z"), _collision("ship", "2026-11-21T11:00:00Z")])
    s = streak(runs)
    assert at_limit(s) and s.run_ids == ("ship-1", "ship-2") and len(s.collisions) == 2
    reason = stalled_reason("intake/ship.md", s)
    assert reason.startswith("decompose of intake/ship.md missed in 2 runs: ship-1, ship-2")
    assert "p1/t2~t1: lib/a.py" in reason


def test_a_clean_decompose_after_a_collided_one_ends_the_streak_and_an_empty_one_still_counts():
    runs = with_collisions([_run("ship-1", 8), _run("ship-2", 10)], [_collision("ship", "2026-11-21T09:00:00Z")])
    assert streak(runs).empty_count == 0
    mixed = with_collisions([_run("ship-1", 8, wrote=False), _run("ship-2", 10)], [_collision("ship", "2026-11-21T11:00:00Z")])
    assert at_limit(streak(mixed))


def test_with_no_collision_the_reason_is_the_empty_decompose_line_it_always_was():
    s = streak([_run("ship-1", 8, wrote=False), _run("ship-2", 10, wrote=False)])
    assert stalled_reason("intake/ship.md", s) == (
        "decompose of intake/ship.md wrote no task items in 2 runs: ship-1, ship-2. no refusal line was printed"
    )


def test_the_tick_reads_every_collision_row_once_by_initiative_from_the_store(tmp_path: Path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    conn = open_store(run_store.store_url_for(runs_dir), "2026-11-21T00:00:00Z")
    try:
        for run_id, initiative, at, kind in (("ship-p1", "ship", "2026-11-21T09:00:00Z", "collision"),
                                              ("dock-p1", "dock", "2026-11-21T08:00:00Z", "collision"),
                                              ("ship-p1", "ship", "2026-11-21T09:30:00Z", "rollup")):
            row = store_steps.step_row(run_id=run_id, step=f"task/t2/{kind}", initiative=initiative, phase="p1",
                                       task_id="t2", kind=kind, round_=0, ok=True, verdict="",
                                       result={"task": "t2", "other": "t1", "files": ["lib/a.py"]}, finished_at=at)
            with conn.transaction():
                store_steps.put_step(conn, row)
    finally:
        conn.close()
    found = chair_decompose_runs.read_collisions(runs_dir)
    assert sorted(found) == ["dock", "ship"] and [r["kind"] for r in found["ship"]] == ["collision"]
    assert chair_decompose_runs.collisions_by_initiative(None) == {}


def test_the_tick_puts_only_the_initiatives_own_collisions_on_its_runs():
    by = {"ship": [_collision("ship", "2026-11-21T09:00:00Z")], "dock": [_collision("dock", "2026-11-21T09:00:00Z")]}
    assert [r.collisions for r in chair_decompose_runs.collided_runs([_run("ship-1", 8)], by, "ship")] == [("p1/t2~t1: lib/a.py",)]
    assert [r.collisions for r in chair_decompose_runs.collided_runs([_run("yard-1", 8)], by, "yard")] == [()]
