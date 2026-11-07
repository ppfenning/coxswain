"""`route edit --epic` and `route epic new` through the real argparse table."""

import pytest
from test_route_edit_epic import INITIATIVE, WITH_EPIC, _setup

from agent_tools import chair_facts, queue_export, queue_rows, run_store
from agent_tools.cli import _epic_definitions, main


@pytest.fixture(autouse=True)
def _runs_beside_the_store(tmp_path, monkeypatch):
    (tmp_path / "runs").mkdir()
    monkeypatch.setattr("agent_tools.cli._runs_dir_for_land", lambda a: (tmp_path / "runs", None))
    monkeypatch.setattr(run_store, "live_lanes", lambda runs_dir, now: [])
    monkeypatch.setattr(run_store, "resolve_initiative", lambda runs_dir, token: token)


def test_the_epic_flag_sets_membership_in_the_row_and_the_file_and_export_holds_nothing(tmp_path, monkeypatch):
    store, path = _setup(tmp_path, monkeypatch)
    assert main(["route", "edit", "ship", "--epic", "big-one"]) == 0
    assert path.read_text(encoding="utf-8") == WITH_EPIC
    assert queue_rows.epic_of_row(store.initiative_row()) == "big-one"
    assert queue_export.read_held(tmp_path, store.rows) == []


def test_epic_none_clears_membership_in_the_row_and_the_file(tmp_path, monkeypatch):
    store, path = _setup(tmp_path, monkeypatch, text=WITH_EPIC)
    assert main(["route", "edit", "ship", "--epic", "none"]) == 0
    assert path.read_text(encoding="utf-8") == INITIATIVE
    assert queue_rows.epic_of_row(store.initiative_row()) is None
    assert queue_export.read_held(tmp_path, store.rows) == []


def test_epic_new_writes_the_file_and_a_second_run_is_refused(tmp_path, capsys):
    argv = ["route", "epic", "new", "big-one", "--title", "Big one", "--target-branch", "release"]
    assert main([*argv, "--dry-run"]) == 0
    assert not (tmp_path / "epics").exists()
    assert main(argv) == 0
    written = (tmp_path / "epics" / "big-one.md").read_text(encoding="utf-8")
    assert written.startswith("---\nid: big-one\n") and "target_branch: release\n" in written
    capsys.readouterr()
    assert main(argv) != 0
    assert (tmp_path / "epics" / "big-one.md").read_text(encoding="utf-8") == written
    assert "big-one" in capsys.readouterr().out


def test_epic_with_an_action_other_than_new_is_refused_with_usage(tmp_path, capsys):
    assert main(["route", "epic", "frob", "big-one"]) == 2
    assert "usage: route epic new" in capsys.readouterr().out
    assert not (tmp_path / "epics").exists()


def test_route_edit_help_lists_the_epic_flag(capsys):
    with pytest.raises(SystemExit) as stop:
        main(["route", "edit", "--help"])
    assert stop.value.code == 0
    assert "--epic" in capsys.readouterr().out


def test_a_created_epic_and_a_joined_initiative_appear_together_in_the_chair_rollup(tmp_path, monkeypatch):
    store, _path = _setup(tmp_path, monkeypatch)
    assert main(["route", "epic", "new", "big-one", "--title", "Big one"]) == 0
    assert main(["route", "edit", "ship", "--epic", "big-one"]) == 0
    (record,) = chair_facts.epic_facts(store.rows, _epic_definitions(tmp_path))
    assert (record["id"], record["title"], [i["id"] for i in record["initiatives"]]) == ("big-one", "Big one", ["ship"])
