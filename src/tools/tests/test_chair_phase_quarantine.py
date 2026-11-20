from agent_tools.chair_phase_quarantine import (
    MARK_KEY,
    mark_run,
    marked_cause,
    marked_extra,
    newest_run,
    phase_targets,
    release_commands,
    row_mark,
)


def test_a_mark_made_with_no_recorded_run_is_not_outdated_by_an_attempt_that_already_existed():
    existing = [{"run": "i1-2", "cause": "code"}]
    run = mark_run([], "i1", [a["run"] for a in existing])
    assert run == "i1-2"
    mark = {"cause": "main_red", "reason": "why", "run": run}
    assert marked_cause(mark, existing) == mark
    assert marked_cause(mark, [*existing, {"run": "i1-3", "cause": "code"}]) is None


def test_the_mark_run_is_the_newest_of_the_recorded_runs_and_the_task_s_own_attempts():
    assert mark_run(["i1-7", "i2-9"], "i1", ["i1-3"]) == "i1-7"
    assert mark_run(["i1-2"], "i1", ["i1-5", "other-8"]) == "i1-5"
    assert mark_run([], "i1", []) == ""


def test_release_commands_name_the_hand_move_per_task():
    assert release_commands("init", ["t1", "t2"]) == [
        "cox route edit t1 --initiative init --state ready", "cox route edit t2 --initiative init --state ready",
    ]
    assert release_commands("init", []) == []


def _row(task: str, state: str, phase: str = "p1", initiative: str = "i1", kind: str = "task") -> dict:
    return {"kind": kind, "initiative": initiative, "task_id": task, "phase": phase, "state": state}


def test_phase_targets_are_the_approved_and_done_tasks_of_that_phase_only():
    rows = [
        _row("a", "approved"), _row("b", "done"), _row("c", "dropped"), _row("d", "in_progress"), _row("e", "ready"),
        _row("f", "quarantined"), _row("g", "approved", phase="p2"), _row("h", "done", initiative="i2"),
        _row("i1", "done", phase="p1", kind="initiative"),
    ]
    assert [r["task_id"] for r in phase_targets(rows, "i1", "p1")] == ["a", "b"]


def test_newest_run_is_the_highest_numbered_run_of_that_initiative():
    assert newest_run(["i1-2", "i1-10", "i1-9", "i10-40", "other-99"], "i1") == "i1-10"
    assert newest_run(["other-1"], "i1") == ""


def test_a_mark_keeps_every_other_extra_key_and_reads_back():
    extra = marked_extra({"k": "v"}, "main_red", "why", "i1-3")
    assert extra == {"k": "v", MARK_KEY: {"cause": "main_red", "reason": "why", "run": "i1-3"}}
    assert row_mark(extra) == {"cause": "main_red", "reason": "why", "run": "i1-3"}
    assert row_mark({}) is None
    assert row_mark(None) is None
    assert row_mark({MARK_KEY: "main_red"}) is None
    assert row_mark({MARK_KEY: {"cause": ""}}) is None


def test_a_mark_holds_until_an_attempt_from_a_later_run_outdates_it():
    mark = {"cause": "main_red", "reason": "why", "run": "i1-3"}
    assert marked_cause(mark, []) == mark
    assert marked_cause(mark, [{"run": "i1-2", "cause": "code"}, {"run": "i1-3", "cause": "code"}]) == mark
    assert marked_cause(mark, [{"run": "i1-4", "cause": "code"}]) is None
    assert marked_cause(None, []) is None
    # A mark made before the initiative had any run is outdated by the first run that fails the task.
    assert marked_cause({**mark, "run": ""}, [{"run": "i1-1", "cause": "code"}]) is None
