from test_chair_plan_withhold import _NOW, _facts, _initiative
from test_route_edit_task import TICKET_FILE, _file, _store, _ticket_row

from agent_tools import chair_read_stranded, route, route_edit, run_store, runs_stranded
from agent_tools.chair_facts import quarantine_facts
from agent_tools.chair_plan import plan_tick

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


def _record(run):
    return {"run": run, "task": "t1", "phase": "p1", "branch": "b1", "review": {"verdict": "approve"},
            "arbitration": {"verdict": "approve"}, "landed": False}


def _rebuild(tmp_path, monkeypatch, runs, store=None, **extra):
    """One forced rebuild of the approved `t1`, with `runs` as the store's run list newest first. Returns the store and the result."""
    store = store or _store(monkeypatch, [_approved_row(**extra)])
    monkeypatch.setattr(run_store, "task_run_phases", lambda runs_dir, initiative, task: runs)
    _file(tmp_path, "work/ship/p1/t1.md", _approved_file(conflict=False))
    result = route_edit.apply_edit(tmp_path, tmp_path, "t1", state="ready", rebuild=True, force=True, live_runs=())
    assert result.code == route_edit.DONE
    return store, result


def test_a_forced_rebuild_lists_every_run_that_holds_the_task(tmp_path, monkeypatch):
    store, _ = _rebuild(tmp_path, monkeypatch, [("ship-2", "p1"), ("ship-1", "p1")])
    assert store.rows[0]["extra"]["rebuilt_from"] == ["ship-2", "ship-1"]
    assert "rebuilt_from:\n- ship-2\n- ship-1\n" in (tmp_path / "work/ship/p1/t1.md").read_text(encoding="utf-8")


def test_a_second_rebuild_keeps_the_runs_the_first_one_listed(tmp_path, monkeypatch):
    store, _ = _rebuild(tmp_path, monkeypatch, [("ship-2", "p1")], rebuilt_from=["ship-1"])
    assert store.rows[0]["extra"]["rebuilt_from"] == ["ship-1", "ship-2"]


def test_a_rebuild_of_an_approved_task_no_run_holds_says_it_marked_nothing(tmp_path, monkeypatch):
    store, result = _rebuild(tmp_path, monkeypatch, [])
    assert "rebuilt_from" not in store.rows[0]["extra"]
    assert "note: no run holds t1; its old approval is not marked superseded" in result.text


def test_two_forced_rebuilds_in_a_row_leave_no_run_stranded_and_the_chair_relaunches(tmp_path, monkeypatch):
    store, _ = _rebuild(tmp_path, monkeypatch, [("ship-1", "p1")])
    store.rows[0]["state"] = "approved"  # ship-2 ran, was approved, and its land failed CI
    _rebuild(tmp_path, monkeypatch, [("ship-2", "p1"), ("ship-1", "p1")], store=store)
    row = store.rows[0]
    assert row["state"] == "ready" and row["extra"]["rebuilt_from"] == ["ship-1", "ship-2"]
    records = [_record("ship-1"), _record("ship-2")]
    stranded = chair_read_stranded.stranded_from_rows(store.rows, records)
    assert stranded == []
    quarantines = quarantine_facts([], stranded, [], set(), lambda initiative, task: False)
    actions = plan_tick(_facts(initiatives=[_initiative("ship", "r1")], quarantines=quarantines), _NOW)
    assert [a["kind"] for a in actions if a["kind"] in {"launch_epic", "needs_chair"}] == ["launch_epic"]
