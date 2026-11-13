import json
from pathlib import Path

from agent_tools.chair_exec import Deps, perform, schedule_document
from agent_tools.chair_schedule import SCHEDULE_FILE

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "dash_feed_schedule_v1.json").read_text())
ACTION = {**FIXTURE["action"], "epoch": 1}  # the planner stamps every action; the fixture's form is stamped here
STANDBY = {"kind": "standby", "epoch": 1}


def _deps(runs_dir):
    return Deps(
        run=lambda argv: (0, ""),
        delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "",
        record=lambda action: None,
        run_id=lambda a: "run-1",
        repo_for=lambda a: "r",
        runs_dir=runs_dir,
    )


def test_schedule_document_is_the_schedule_object_alone():
    assert json.loads(schedule_document({"kind": "schedule_updated", "schedule": {"b": 1, "a": []}})) == {"b": 1, "a": []}


def test_apply_writes_the_fixture_schedule(tmp_path):
    results = perform([ACTION], _deps(tmp_path), lambda: 1, False)
    assert results[0]["status"] == "done"
    assert json.loads((tmp_path / SCHEDULE_FILE).read_text()) == FIXTURE["action"]["schedule"] == FIXTURE["schedule"]
    assert [p.name for p in tmp_path.iterdir()] == [SCHEDULE_FILE]


def test_written_document_is_readable_by_other_users(tmp_path):
    perform([ACTION], _deps(tmp_path), lambda: 1, False)
    assert (tmp_path / SCHEDULE_FILE).stat().st_mode & 0o777 == 0o644


def test_second_apply_replaces_the_document(tmp_path):
    newer = {**ACTION, "schedule": {"lanes": [], "initiatives": []}}
    perform([ACTION], _deps(tmp_path), lambda: 1, False)
    perform([newer], _deps(tmp_path), lambda: 1, False)
    assert json.loads((tmp_path / SCHEDULE_FILE).read_text()) == newer["schedule"]
    assert [p.name for p in tmp_path.iterdir()] == [SCHEDULE_FILE]


def test_unwritable_directory_fails_and_the_next_action_runs(tmp_path):
    results = perform([ACTION, STANDBY], _deps(tmp_path / "missing"), lambda: 1, False)
    assert [r["status"] for r in results] == ["failed", "recorded"]
    assert SCHEDULE_FILE in results[0]["reason"]


def test_stale_epoch_is_fenced_and_leaves_the_document_alone(tmp_path):
    perform([ACTION], _deps(tmp_path), lambda: 1, False)
    stale = {**ACTION, "epoch": 0, "schedule": {"lanes": [], "initiatives": []}}
    results = perform([stale], _deps(tmp_path), lambda: 1, False)
    assert results[0]["status"] == "fenced"
    assert json.loads((tmp_path / SCHEDULE_FILE).read_text()) == ACTION["schedule"]


def test_unstamped_action_is_fenced_and_writes_nothing(tmp_path):
    results = perform([FIXTURE["action"]], _deps(tmp_path), lambda: 1, False)
    assert results[0]["status"] == "fenced"
    assert list(tmp_path.iterdir()) == []


def test_action_without_a_schedule_is_refused(tmp_path):
    results = perform([{"kind": "schedule_updated", "epoch": 1}], _deps(tmp_path), lambda: 1, False)
    assert results[0]["status"] == "refused"
    assert list(tmp_path.iterdir()) == []


def test_dry_run_writes_nothing(tmp_path):
    results = perform([ACTION], _deps(tmp_path), lambda: 1, True)
    assert results[0]["status"] == "dry_run"
    assert list(tmp_path.iterdir()) == []
