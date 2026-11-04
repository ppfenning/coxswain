from agent_tools.route_guard import fresh_id_note, fresh_initiative_id


def _initiative(task_id, state):
    return {"kind": "initiative", "task_id": task_id, "state": state}


def test_no_initiative_row_returns_the_id():
    rows = [{"kind": "task", "task_id": "widgets", "state": "dropped"}]
    assert fresh_initiative_id("widgets", rows) == "widgets"
    assert fresh_initiative_id("widgets", []) == "widgets"


def test_live_initiative_keeps_the_id():
    assert fresh_initiative_id("widgets", [_initiative("widgets", "ready")]) == "widgets"


def test_dropped_initiative_gets_suffix_two():
    assert fresh_initiative_id("widgets", [_initiative("widgets", "dropped")]) == "widgets-2"


def test_dropped_initiative_and_taken_suffix_two_gets_suffix_three():
    rows = [_initiative("widgets", "dropped"), _initiative("widgets-2", "done")]
    assert fresh_initiative_id("widgets", rows) == "widgets-3"


def test_note_is_empty_when_unchanged():
    assert fresh_id_note("widgets", "widgets") == ""


def test_note_names_both_ids_when_changed():
    assert (
        fresh_id_note("widgets", "widgets-2") == "initiative widgets is dropped, so the next decompose uses widgets-2"
    )
