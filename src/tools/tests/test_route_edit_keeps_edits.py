from test_route_edit_rebuild import CONFLICT, _approved_file, _approved_row
from test_route_edit_task import INTAKE, TICKET_FILE, _file, _intake_row, _store, _ticket_row

from agent_tools import route_edit

REL = "work/ship/p1/t1.md"
HAND_BODY = "Hand body\n\nSecond paragraph\n"
HAND_NEEDS = "needs:\n- other\n- extra\n"


def _blocked_row():
    return {**_ticket_row(), "state": "blocked"}


def _hand_edited(text=TICKET_FILE, state="blocked"):
    """The ticket file with an operator's needs and body written over the stored ones."""
    return (
        text.replace("state: todo", f"state: {state}")
        .replace("needs:\n- other\n", HAND_NEEDS)
        .replace("Old body\n", HAND_BODY)
    )


def test_hand_edited_needs_and_body_survive_a_move_to_ready(tmp_path, monkeypatch):
    store = _store(monkeypatch, [_blocked_row()])
    path = _file(tmp_path, REL, _hand_edited())
    result = route_edit.apply_edit(tmp_path, tmp_path, "t1", state="ready", live_runs=())
    assert result.code == route_edit.DONE
    assert store.upserts == [{"initiative": "ship", "task_id": "t1", "body": f"\n{HAND_BODY}", "needs": ["other", "extra"]}]
    assert [state for _, _, state, _, _ in store.states] == ["ready"]
    assert path.read_text(encoding="utf-8") == _hand_edited(state="ready")


def test_the_output_names_what_was_kept(tmp_path, monkeypatch):
    _store(monkeypatch, [_blocked_row()])
    _file(tmp_path, REL, _hand_edited())
    result = route_edit.apply_edit(tmp_path, tmp_path, "t1", state="ready", live_runs=())
    assert result.text.splitlines()[-1] == "kept from the file: body and needs"
    _store(monkeypatch, [_blocked_row()])
    _file(tmp_path, REL, TICKET_FILE.replace("state: todo", "state: blocked").replace("Old body", "Only body"))
    only_body = route_edit.apply_edit(tmp_path, tmp_path, "t1", state="ready", live_runs=())
    assert only_body.text.splitlines()[-1] == "kept from the file: body"


def test_a_file_equal_to_the_row_reports_nothing_kept(tmp_path, monkeypatch):
    _store(monkeypatch, [_blocked_row()])
    _file(tmp_path, REL, TICKET_FILE.replace("state: todo", "state: blocked"))
    result = route_edit.apply_edit(tmp_path, tmp_path, "t1", state="ready", live_runs=())
    assert result.code == route_edit.DONE and "kept" not in result.text


def test_a_rebuild_keeps_the_hand_edit_and_still_appends_its_note(tmp_path, monkeypatch):
    store = _store(monkeypatch, [_approved_row(land_conflict=CONFLICT)])
    path = _file(tmp_path, REL, _hand_edited(_approved_file(), state="approved"))
    result = route_edit.apply_edit(tmp_path, tmp_path, "t1", state="ready", rebuild=True, live_runs=())
    assert result.code == route_edit.DONE and "kept from the file: body and needs" in result.text
    [row] = store.rows
    assert row["needs"] == ["other", "extra"]
    assert row["body"] == f"\n{HAND_BODY}\nLand conflict with main@abc12345 in cli.py; rebuilding on current main.\n"
    assert "- extra\n" in path.read_text(encoding="utf-8") and "Second paragraph" in path.read_text(encoding="utf-8")


def test_an_explicit_body_with_a_differing_file_is_refused_with_a_diff_and_writes_nothing(tmp_path, monkeypatch):
    store = _store(monkeypatch, [_blocked_row()])
    text = _hand_edited()
    path = _file(tmp_path, REL, text)
    result = route_edit.apply_edit(tmp_path, tmp_path, "t1", body="Typed body", state="ready", live_runs=())
    assert result.code == route_edit.REFUSED
    assert "-Old body\n" in result.text and "+Hand body\n" in result.text and "+needs: other, extra\n" in result.text
    assert store.states == [] and store.upserts == [] and store.hashes == []
    assert path.read_text(encoding="utf-8") == text


def test_an_explicit_body_file_is_refused_the_same_way(tmp_path, monkeypatch):
    store = _store(monkeypatch, [_blocked_row()])
    _file(tmp_path, REL, _hand_edited())
    body_file = _file(tmp_path, "body.txt", "Typed body\n")
    result = route_edit.apply_edit(tmp_path, tmp_path, "t1", body_file=str(body_file), state="ready", live_runs=())
    assert result.code == route_edit.REFUSED and "+Hand body\n" in result.text
    assert store.states == [] and store.upserts == []


def test_an_explicit_body_with_a_file_equal_to_the_row_is_applied(tmp_path, monkeypatch):
    store = _store(monkeypatch, [_blocked_row()])
    _file(tmp_path, REL, TICKET_FILE.replace("state: todo", "state: blocked"))
    result = route_edit.apply_edit(tmp_path, tmp_path, "t1", body="Typed body", state="ready", live_runs=())
    assert result.code == route_edit.DONE and store.rows[0]["body"] == "\nTyped body\n"


def test_a_note_is_appended_once(tmp_path, monkeypatch):
    store = _store(monkeypatch, [_ticket_row()])
    path = _file(tmp_path, REL, TICKET_FILE)
    first = route_edit.apply_edit(tmp_path, tmp_path, "t1", note="Watch the flaky test", live_runs=())
    assert first.code == route_edit.DONE and first.text.splitlines()[-1] == "note appended: Watch the flaky test"
    assert store.rows[0]["body"] == "\nOld body\n\nWatch the flaky test\n"
    again = route_edit.apply_edit(tmp_path, tmp_path, "t1", note="Watch the flaky test", live_runs=())
    assert again.code == route_edit.DONE and again.text == "note already in the body\n"
    assert store.rows[0]["body"].count("Watch the flaky test") == 1
    assert path.read_text(encoding="utf-8").count("Watch the flaky test") == 1


def test_a_note_goes_with_a_state_move_on_top_of_the_kept_body(tmp_path, monkeypatch):
    store = _store(monkeypatch, [_blocked_row()])
    _file(tmp_path, REL, _hand_edited())
    result = route_edit.apply_edit(tmp_path, tmp_path, "t1", state="ready", note="Back in the queue", live_runs=())
    assert result.code == route_edit.DONE
    assert store.rows[0]["body"] == f"\n{HAND_BODY}\nBack in the queue\n"
    assert result.text.splitlines()[-2:] == ["kept from the file: body and needs", "note appended: Back in the queue"]


def test_a_note_is_refused_on_an_intake_and_on_an_initiative(tmp_path, monkeypatch):
    store = _store(monkeypatch, [_intake_row(), _ticket_row()])
    path = _file(tmp_path, "intake/fix-it.md", INTAKE)
    refused = route_edit.apply_edit(tmp_path, tmp_path, "fix-it", note="x", live_runs=())
    assert refused.code == route_edit.REFUSED and "--note applies to a task ticket" in refused.text
    assert path.read_text(encoding="utf-8") == INTAKE and store.upserts == []
    initiative = _file(tmp_path, "work/ship/initiative.md", "---\ntitle: Ship\n---\n\nBody\n")
    refused = route_edit.apply_edit(tmp_path, tmp_path, "ship", note="x", live_runs=())
    assert refused.code == route_edit.REFUSED and "--note applies to a task ticket" in refused.text
    assert initiative.read_text(encoding="utf-8") == "---\ntitle: Ship\n---\n\nBody\n"


def test_a_row_with_no_needs_does_not_take_them_back_from_the_file():
    stored = {**_blocked_row(), "needs": []}
    assert route_edit.file_edit(stored, REL, TICKET_FILE.replace("state: todo", "state: blocked")) is None
    assert route_edit.file_edit(stored, REL, _hand_edited()) == {"body": f"\n{HAND_BODY}"}


def test_check_options_takes_a_lone_note_and_refuses_it_beside_a_body_or_empty():
    assert route_edit.check_options(None, None, None, None, note="n") is None
    assert "--note" in route_edit.check_options(None, None, None, None)
    assert route_edit.check_options(None, "b", None, None, note="n") == "--note appends to the body; give it without --body or --body-file"
    assert route_edit.check_options(None, None, None, None, note=" ") == "--note must not be empty"
