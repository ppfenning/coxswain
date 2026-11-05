from agent_tools import land

BRANCH = "card/x/wire"


def _phase(**extra):
    return {"run": "epic-x-5", "phase": "seams", "initiative": "x", "card_branch": BRANCH, "last_phase": True, **extra}


def _task(task, status="approved", repo="wire"):
    return {"task": task, "title": task, "repo": repo, "status": status, "review": {"verdict": "approve"}}


def _plan(record, ids, records, ticket_tasks, **extra):
    items = [{"id": t, "status": "approved"} for t in ids]
    return land.land_plan(record, {}, "main", items=items, task_records=records, repo="wire", ticket_tasks=ticket_tasks, **extra)


def _kinds(steps):
    return [s["kind"] for s in steps]


def _marks(steps):
    return {s["task"]: s for s in steps if s["kind"] == "mark_done"}


def test_an_unrecorded_earlier_run_task_is_marked_with_recorded_false_after_the_merge():
    steps = _plan(_phase(), "a", [_task("a")], ("a", "e"), ticket_unrecorded=("e",))
    marks = _marks(steps)
    assert marks["e"] == {"kind": "mark_done", "task": "e", "recorded": False}
    assert marks["a"] == {"kind": "mark_done", "task": "a"}
    assert "note" not in _kinds(steps)
    assert _kinds(steps).index("merge") < _kinds(steps).index("mark_done")


def test_an_already_merged_ticket_plan_marks_every_ticket_task():
    records = [{**_task("a"), "arbitration": {"verdict": "approve"}}]
    steps = _plan(_phase(card_branch=""), "a", records, ("a", "b", "e"), already_merged=True, ticket_unrecorded=("e",))
    assert _kinds(steps)[0] == "clean_phase"
    assert list(_marks(steps)) == ["a", "b", "e"]
    assert _marks(steps)["e"]["recorded"] is False and "recorded" not in _marks(steps)["a"]
    assert "note" not in _kinds(steps)


def test_an_already_merged_level_plan_marks_a_phase_item_with_no_record():
    records = [{**_task("a"), "arbitration": {"verdict": "approve"}}]
    steps = _plan(_phase(card_branch="", land_at="level", last_phase=False), "ab", records, ())
    assert _kinds(steps) == ["refuse"]
    steps = _plan(_phase(card_branch="", land_at="level", last_phase=False), "ab", records, (), already_merged=True)
    assert _kinds(steps) == ["clean_phase", "mark_done", "mark_done"]
    assert _marks(steps)["a"] == {"kind": "mark_done", "task": "a"}
    assert _marks(steps)["b"] == {"kind": "mark_done", "task": "b", "recorded": False}


def test_a_recorded_task_whose_work_is_absent_is_still_left_with_a_note():
    records = [_task("a"), _task("b", repo="elsewhere")]
    steps = _plan(_phase(), "ab", records, ("a", "b", "e"), ticket_unrecorded=("e",))
    assert list(_marks(steps)) == ["a", "e"]
    assert [s["unmerged"] for s in steps if s["kind"] == "note"] == [["b"]]
