from test_route_edit_task import TICKET_FILE, _file, _store, _ticket_row

from agent_tools import route, route_edit, runs_stranded

CONFLICT = "land conflict with main@abc12345 in cli.py"
REFUSAL = "the ticket is 'approved'; moving it back to 'ready' could launch it again"


def _approved_row(**extra):
    return {**_ticket_row(), "state": "approved", "extra": {**_ticket_row()["extra"], **extra}}


def _approved_file(conflict=True):
    line = f"land_conflict: {CONFLICT}\n" if conflict else ""
    return TICKET_FILE.replace("state: todo\n", f"state: approved\n{line}")


def test_rebuild_moves_an_approved_task_with_a_recorded_conflict_back_to_ready(tmp_path, monkeypatch):
    store = _store(monkeypatch, [_approved_row(land_conflict=CONFLICT)])
    path = _file(tmp_path, "work/ship/p1/t1.md", _approved_file())
    result = route_edit.apply_edit(tmp_path, tmp_path, "t1", state="ready", rebuild=True, live_runs=())
    assert result.code == route_edit.DONE
    assert store.states == [("ship", "t1", "ready", route_edit.EDITED_BY, "approved")]
    [row] = store.rows
    assert row["body"] == "\nOld body\n\nLand conflict with main@abc12345 in cli.py; rebuilding on current main.\n"
    assert "land_conflict" not in row["extra"] and row["task_id"] == "t1"
    assert path.read_text(encoding="utf-8").endswith("rebuilding on current main.\n")


def test_rebuild_without_a_recorded_conflict_is_refused_unless_forced(tmp_path, monkeypatch):
    store = _store(monkeypatch, [_approved_row()])
    _file(tmp_path, "work/ship/p1/t1.md", _approved_file(conflict=False))
    refused = route_edit.apply_edit(tmp_path, tmp_path, "t1", state="ready", rebuild=True, live_runs=())
    assert refused.code == route_edit.REFUSED
    assert REFUSAL in refused.text and "--force" in refused.text
    assert store.states == [] and store.upserts == []
    forced = route_edit.apply_edit(tmp_path, tmp_path, "t1", state="ready", rebuild=True, force=True, live_runs=())
    assert forced.code == route_edit.DONE and store.states[0][2] == "ready"
    assert store.rows[0]["body"].endswith("Rebuild forced from approved with --force; no land conflict was recorded.\n")


def test_a_plain_backward_move_keeps_its_refusal_even_with_a_recorded_conflict(tmp_path, monkeypatch):
    store = _store(monkeypatch, [_approved_row(land_conflict=CONFLICT)])
    _file(tmp_path, "work/ship/p1/t1.md", _approved_file())
    result = route_edit.apply_edit(tmp_path, tmp_path, "t1", state="ready", live_runs=())
    assert result.code == route_edit.REFUSED and result.text == REFUSAL
    assert store.states == []


def test_rebuild_and_force_are_refused_without_the_state_they_go_with():
    assert route_edit.check_options(None, None, None, None, rebuild=True) == "--rebuild applies only with --state ready"
    assert route_edit.check_options(None, None, None, None, state="done", rebuild=True) == "--rebuild applies only with --state ready"
    assert route_edit.check_options(None, None, None, None, state="ready", force=True) == "--force applies only with --rebuild"
    assert route_edit.check_options(None, None, None, None, state="ready", rebuild=True) is None


def test_a_rebuilt_task_is_what_a_relaunch_picks_up(tmp_path, monkeypatch):
    store = _store(monkeypatch, [{**_approved_row(land_conflict=CONFLICT), "needs": []}])
    _file(tmp_path, "work/ship/p1/t1.md", _approved_file())
    assert route.ready_tasks(store.rows) == []
    route_edit.apply_edit(tmp_path, tmp_path, "t1", state="ready", rebuild=True, live_runs=())
    assert [row["task_id"] for row in route.ready_tasks(store.rows)] == ["t1"]


def test_an_item_with_a_recorded_land_conflict_is_listed_as_a_rebuild_not_a_land():
    record = {"run": "r1", "task": "t1", "phase": "p1", "branch": "b1", "review": {"verdict": "approve"},
              "arbitration": {"verdict": "approve"}, "landed": False}
    item = {"id": "t1", "state": "ready", "initiative": "acme", "phase": "p1", "repo": "/repo/acme"}
    [row] = runs_stranded.stranded([record], [{**item, "land_conflict": CONFLICT}])
    assert row["remedy"] == f"rebuild: {CONFLICT}"
    assert runs_stranded.stranded([record], [item])[0]["remedy"].startswith("cox runs land ")
    reapproved = {**item, "state": "approved", "land_conflict": CONFLICT}
    assert runs_stranded.stranded([record], [reapproved])[0]["remedy"].startswith("cox runs land ")
