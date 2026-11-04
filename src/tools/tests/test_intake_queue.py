import copy

from agent_tools.intake_queue import (
    after_ids,
    held_rows,
    intake_priority,
    is_held,
    landed_ids,
    runnable_intake,
    unmet_after,
    waiting_on,
)


def task(initiative: str, state: str, task_id: str = "t") -> dict:
    return {"kind": "task", "initiative": initiative, "task_id": task_id, "state": state, "extra": {}}


def intake(task_id: str, state: str = "queued", **extra: object) -> dict:
    return {"kind": "intake", "initiative": "intake", "task_id": task_id, "state": state, "extra": dict(extra)}


def test_after_ids_list_string_absent_and_blank() -> None:
    assert after_ids({"after": ["a", " b ", "", 3]}) == ["a", "b"]
    assert after_ids({"after": "solo"}) == ["solo"]
    assert after_ids({}) == []


def test_is_held_for_true_as_a_bool_or_the_string_the_store_writes() -> None:
    assert is_held({"held": True}) is True
    assert is_held({"held": "true"}) is True
    assert is_held({}) is False
    assert is_held({"held": "false"}) is False
    assert is_held({"held": False}) is False


def test_intake_priority() -> None:
    assert intake_priority({"priority": 5}) == 5
    assert intake_priority({}) == 0
    assert intake_priority({"priority": "high"}) == 0
    assert intake_priority({"priority": True}) == 0


def test_initiative_landed_when_all_tasks_done_or_dropped() -> None:
    rows = [task("a", "done", "1"), task("a", "dropped", "2")]
    assert landed_ids(rows) == {"a"}


def test_initiative_with_only_dropped_tasks_has_landed() -> None:
    assert landed_ids([task("a", "dropped")]) == {"a"}


def test_initiative_with_open_task_has_not_landed() -> None:
    assert landed_ids([task("a", "done", "1"), task("a", "ready", "2")]) == set()


def test_initiative_with_no_tasks_has_not_landed() -> None:
    initiative = {"kind": "initiative", "initiative": "a", "task_id": "a", "state": "active", "extra": {}}
    assert landed_ids([initiative]) == set()


def test_intake_landed_through_extra_initiative() -> None:
    rows = [task("a", "done"), intake("idea", "decomposed", initiative="a")]
    assert landed_ids(rows) == {"a", "idea"}


def test_landed_intake_holds_both_ids() -> None:
    rows = [task("a", "done"), intake("file-name", "decomposed", id="field-id", initiative="a")]
    assert landed_ids(rows) == {"a", "file-name", "field-id"}


def test_removed_intake_never_lands() -> None:
    rows = [task("a", "done"), intake("gone", "removed", initiative="a")]
    assert landed_ids(rows) == {"a"}


def test_after_naming_extra_id_is_met() -> None:
    rows = [
        task("a", "done"),
        intake("file-name", "decomposed", id="field-id", initiative="a"),
        intake("next", after="field-id"),
    ]
    assert unmet_after(rows[2], rows) == []
    assert runnable_intake(rows) == [rows[2]]


def test_after_naming_task_id_is_met() -> None:
    rows = [
        task("a", "done"),
        intake("file-name", "decomposed", id="field-id", initiative="a"),
        intake("next", after="file-name"),
    ]
    assert unmet_after(rows[2], rows) == []
    assert runnable_intake(rows) == [rows[2]]


def test_unmet_after_string_unknown_and_partial() -> None:
    rows = [task("a", "done"), intake("x", after="nowhere"), intake("y", after=["a", "nowhere", "b"])]
    assert unmet_after(rows[1], rows) == ["nowhere"]
    assert unmet_after(rows[2], rows) == ["nowhere", "b"]


def test_runnable_orders_by_priority_with_ties_in_row_order() -> None:
    rows = [intake("low"), intake("tie1", priority=2), intake("top", priority=9), intake("tie2", priority=2)]
    assert [r["task_id"] for r in runnable_intake(rows)] == ["top", "tie1", "tie2", "low"]


def test_runnable_excludes_held_even_with_satisfied_after() -> None:
    rows = [task("a", "done"), intake("h", held=True, after="a"), intake("ok", after="a")]
    assert [r["task_id"] for r in runnable_intake(rows)] == ["ok"]


def test_runnable_drops_unmet_and_non_queued_and_does_not_mutate() -> None:
    rows = [intake("blocked", after="nowhere"), intake("done-one", "done"), intake("ok")]
    before = copy.deepcopy(rows)
    result = runnable_intake(rows)
    assert [r["task_id"] for r in result] == ["ok"]
    assert result is not rows
    assert rows == before


def test_waiting_on_and_held_rows() -> None:
    rows = [intake("w", after=["nowhere"]), intake("h", held=True, after="nowhere"), intake("free")]
    assert waiting_on(rows) == {"w": ["nowhere"]}
    assert [r["task_id"] for r in held_rows(rows)] == ["h"]
