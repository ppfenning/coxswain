from agent_tools.chair_facts import chat_epics


def _task(initiative: str, task_id: str, phase: str, state: str) -> dict:
    return {"kind": "task", "initiative": initiative, "task_id": task_id, "phase": phase, "state": state}


def test_phases_come_in_natural_order_with_each_task_under_its_own_phase():
    rows = [_task("a", "t10", "p10", "ready"), _task("a", "t2", "p2", "approved"), _task("a", "t1", "p1", "done"), _task("a", "t2b", "p2", "ready")]
    [epic] = chat_epics(rows, {})
    assert epic == {
        "epic_id": "a",
        "levels": [
            {"phase": "p1", "tasks": [{"id": "t1", "state": "done"}]},
            {"phase": "p2", "tasks": [{"id": "t2", "state": "approved"}, {"id": "t2b", "state": "ready"}]},
            {"phase": "p10", "tasks": [{"id": "t10", "state": "ready"}]},
        ],
        "exited": False,
    }


def test_a_store_state_outside_the_views_words_reads_building_and_the_views_own_words_are_kept():
    states = ("todo", "blocked", "in_progress", "ready", "approved", "done", "quarantined", "dropped")
    [epic] = chat_epics([_task("a", s, "p1", s) for s in states], {})
    assert {t["id"]: t["state"] for t in epic["levels"][0]["tasks"]} == {
        "todo": "building", "blocked": "building", "in_progress": "building",
        "ready": "ready", "approved": "approved", "done": "done", "quarantined": "quarantined", "dropped": "dropped",
    }


def test_an_initiative_whose_tasks_are_all_done_or_dropped_is_left_out():
    rows = [_task("closed", "c1", "p1", "done"), _task("closed", "c2", "p1", "dropped"), _task("open", "o1", "p1", "done"), _task("open", "o2", "p2", "todo")]
    assert [e["epic_id"] for e in chat_epics(rows, {})] == ["open"]
    assert chat_epics(rows[:2], {}) == []


def test_exited_comes_from_the_run_exited_mapping_and_is_false_for_an_initiative_it_does_not_name():
    rows = [_task("a", "a1", "p1", "ready"), _task("b", "b1", "p1", "ready"), _task("c", "c1", "p1", "ready")]
    assert {e["epic_id"]: e["exited"] for e in chat_epics(rows, {"a": True, "b": False})} == {"a": True, "b": False, "c": False}


def test_rows_of_a_kind_other_than_task_are_ignored():
    rows = [
        {"kind": "initiative", "initiative": "a", "task_id": "a", "phase": "", "state": "todo"},
        {"kind": "phase", "initiative": "a", "task_id": "p1", "phase": "p1", "state": "todo"},
        {"kind": "initiative", "initiative": "b", "task_id": "b", "phase": "", "state": "todo"},
        _task("a", "t1", "p1", "ready"),
    ]
    assert chat_epics(rows, {}) == [{"epic_id": "a", "levels": [{"phase": "p1", "tasks": [{"id": "t1", "state": "ready"}]}], "exited": False}]
