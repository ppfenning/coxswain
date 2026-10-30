import subprocess

import pytest
from test_route_edit_rebuild import CONFLICT, _approved_file, _approved_row
from test_route_edit_task import _file, _store

from agent_tools import cli, land, queue_export, route, route_edit, store_cli

HEAD = {
    "kind": "initiative", "initiative": "ship", "task_id": "ship", "phase": "", "state": "ready", "needs": [], "title": "Ship",
    "surfaces": [], "body": "", "extra": {"phases": [{"id": "p1", "goal": "g"}], "repo": "/r"}, "holder": None, "epoch": None,
    "expires_at": None,
}
RECORD = {"initiative": "ship", "task": "t1"}


def _land_conflicted(tmp_path, monkeypatch, mode):
    """An approved task with no needs, in a store double, after a land that conflicted: the edge `_runs_land` calls."""
    store = _store(monkeypatch, [HEAD, {**_approved_row(), "needs": []}])
    mirror = lambda runs_dir, initiative, task, state, by: (store.set_state(runs_dir, initiative, task, state, by), None)[1]  # noqa: E731
    monkeypatch.setattr(store_cli, "mirror_state", mirror)
    monkeypatch.setattr(queue_export, "record_written_paths", lambda root, paths: None)
    item = _file(tmp_path, "work/ship/p1/t1.md", _approved_file(conflict=False))
    cli._mark_land_conflict(tmp_path / "runs", RECORD, str(item), CONFLICT, "me", mode)
    return store, item


@pytest.mark.parametrize(("mode", "expected"), [("store", "approved"), ("files", None)])
def test_a_conflicted_land_moves_the_task_out_of_approved_and_records_the_conflict(tmp_path, monkeypatch, mode, expected):
    store, item = _land_conflicted(tmp_path, monkeypatch, mode)
    assert store.states == [("ship", "t1", "ready", "me", expected)]
    task = next(row for row in store.rows if row["task_id"] == "t1")
    assert task["state"] == "ready" and task["extra"]["land_conflict"] == CONFLICT
    assert "state: ready\nland_conflict: land conflict with main@abc12345 in cli.py\n" in item.read_text(encoding="utf-8")


def test_a_store_that_refuses_the_move_leaves_the_file_and_the_row_alone(tmp_path, monkeypatch):
    done = {**_approved_row(), "state": "done"}
    store = _store(monkeypatch, [HEAD, dict(done)])
    item = _file(tmp_path, "work/ship/p1/t1.md", _approved_file(conflict=False))
    cli._mark_land_conflict(tmp_path / "runs", RECORD, str(item), CONFLICT, "me", "store")
    assert store.rows[1] == done and item.read_text(encoding="utf-8") == _approved_file(conflict=False)


def test_a_crash_after_the_row_is_marked_leaves_an_approved_task_that_rebuild_accepts(tmp_path, monkeypatch):
    store = _store(monkeypatch, [HEAD, {**_approved_row(), "needs": []}])
    _file(tmp_path, "work/ship/p1/t1.md", _approved_file(conflict=False))

    def crash(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(store_cli, "set_state", crash)
    with pytest.raises(KeyboardInterrupt):
        cli._mark_land_conflict(tmp_path / "runs", RECORD, None, CONFLICT, "me", "store")
    task = store.rows[1]
    assert task["state"] == "approved" and task["extra"]["land_conflict"] == CONFLICT
    monkeypatch.setattr(store_cli, "set_state", store.set_state)
    result = route_edit.apply_edit(tmp_path, tmp_path, "t1", state="ready", rebuild=True, live_runs=())
    assert result.code == route_edit.DONE and task["state"] == "ready" and "land_conflict" not in task["extra"]
    assert task["body"].count("rebuilding on current main.") == 1


def _conflicted_repo(tmp_path):
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.com"), ("config", "user.name", "t")):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True)
    (tmp_path / "f").write_text("a\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    return subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout[:8]


def test_a_land_that_stopped_on_its_cherry_pick_marks_the_task_with_the_tip_of_main(tmp_path, monkeypatch):
    (tmp_path / "repo").mkdir()
    tip = _conflicted_repo(tmp_path / "repo")
    store = _store(monkeypatch, [HEAD, {**_approved_row(), "needs": []}])
    detail = "cherry-pick of abcd1234 conflicts in: cli.py, f\ncould not apply abcd1234"
    stops = [land.stop_fields("cherry_pick", detail, None, None)]
    cli._mark_stopped_land(tmp_path / "repo", tmp_path / "runs", 1, RECORD, None, stops, {"from": "main"}, "me", "store")
    task = store.rows[1]
    assert task["state"] == "ready" and task["extra"]["land_conflict"] == f"land conflict with main@{tip} in cli.py, f"


@pytest.mark.parametrize("rc, kind", [(2, "cherry_pick"), (1, "checks")])
def test_a_land_that_did_not_stop_on_a_cherry_pick_conflict_marks_nothing(tmp_path, monkeypatch, rc, kind):
    store = _store(monkeypatch, [HEAD, {**_approved_row(), "needs": []}])
    stops = [land.stop_fields(kind, "cherry-pick of abcd1234 conflicts in: f", None, None)]
    cli._mark_stopped_land(tmp_path, tmp_path / "runs", rc, RECORD, None, stops, {"from": "main"}, "me", "store")
    assert store.states == [] and store.upserts == []


def test_a_relaunch_finds_nothing_before_the_land_conflict_and_the_task_after_it(tmp_path, monkeypatch):
    store = _store(monkeypatch, [HEAD, {**_approved_row(), "needs": []}])
    assert route.launch_from_rows(tmp_path / "runs", "ship", "/r")[2]["ready"] == []
    cli._mark_land_conflict(tmp_path / "runs", RECORD, None, CONFLICT, "me", "store")
    launch = route.launch_from_rows(tmp_path / "runs", "ship", "/r")[2]
    assert launch["ready"] == ["t1"]
    [built] = [task for task in launch["tasks"] if task["id"] == "t1"]
    assert "Land conflict with main@abc12345 in cli.py; rebuilding on current main." in built["body"]
    assert store.rows[1]["extra"]["land_conflict"] == CONFLICT


def test_a_rebuilt_task_approved_again_is_stranded_for_a_land_not_a_rebuild(tmp_path, monkeypatch):
    from agent_tools import chair_read_stranded

    store = _store(monkeypatch, [HEAD, {**_approved_row(), "needs": []}])
    cli._mark_land_conflict(tmp_path / "runs", RECORD, None, CONFLICT, "me", "store")
    record = {"run": "r2", "task": "t1", "initiative": "ship", "phase": "p1", "branch": "b", "review": {"verdict": "approve"},
              "arbitration": {"verdict": "approve"}, "landed": False, "repo": "/r"}
    assert chair_read_stranded.stranded_from_rows(store.rows, [record])[0]["remedy"] == f"rebuild: {CONFLICT}"
    store.rows[1]["state"] = "approved"  # the relaunch built it and it was approved again, marker untouched
    assert chair_read_stranded.stranded_from_rows(store.rows, [record])[0]["remedy"].startswith("cox runs land ")


def test_rebuild_then_files_the_note_and_clears_the_marker_on_the_task_the_land_left_ready(tmp_path, monkeypatch):
    store, _ = _land_conflicted(tmp_path, monkeypatch, "store")
    result = route_edit.apply_edit(tmp_path, tmp_path, "t1", state="ready", rebuild=True, live_runs=())
    task = next(row for row in store.rows if row["task_id"] == "t1")
    assert result.code == route_edit.DONE and store.states == [("ship", "t1", "ready", "me", "approved")]
    assert task["body"].endswith("Land conflict with main@abc12345 in cli.py; rebuilding on current main.\n")
    assert "land_conflict" not in task["extra"]
    assert route.launch_from_rows(tmp_path / "runs", "ship", "/r")[2]["ready"] == ["t1"]
