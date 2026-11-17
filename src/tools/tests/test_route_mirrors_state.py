"""`route file` sets `state: ready` on a new work item, so it mirrors that state to the store.

Route sync only pushes state to GitHub Projects and never writes `state:` to a file, so it has nothing to mirror.
"""
from pathlib import Path

import pytest

from agent_tools import cli, route, store_cli
from agent_tools.cli import main


@pytest.fixture(autouse=True)
def _no_gh_sync_on_file(monkeypatch):
    monkeypatch.setattr("agent_tools.cli._sync_filed_items", lambda *_a, **_k: None)


def _profile(tmp_path: Path):
    (tmp_path / "widget" / ".git").mkdir(parents=True)
    ws = tmp_path / "workspace"
    ws.mkdir()
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        f"team: acme\nworkspace_dir: {ws}\nharness_dir: /opt/h\ncartridges_dir: /opt/c\nprovider_profile: /opt/p.yaml\n"
    )
    return profile, ws


def _file(profile: Path, *extra: str) -> int:
    repo = profile.parent / "widget"
    return main(["route", "file", "--profile", str(profile), "--repo", str(repo), "--title", "Fix the thing", *extra])


def test_a_filed_item_mirrors_ready_with_the_right_arguments_after_the_write(tmp_path, monkeypatch):
    profile, ws = _profile(tmp_path)
    monkeypatch.setenv("COX_SESSION_LABEL", "leader-a")
    seen = []

    def record(runs_dir, initiative, task, state, by):
        seen.append((runs_dir, initiative, task, state, by, (ws / "work/fix-the-thing/build/fix-the-thing-task.md").read_text()))

    monkeypatch.setattr("agent_tools.store_cli.mirror_state", record)
    assert _file(profile) == 0
    repo = str(tmp_path / "widget")
    task_text = route.initiative_files("Fix the thing", "", repo)["work/fix-the-thing/build/fix-the-thing-task.md"]
    assert seen == [(ws / "runs", "fix-the-thing", "fix-the-thing-task", "ready", "leader-a", task_text)]


def test_a_real_store_takes_the_mirror_and_nothing_extra_is_printed(tmp_path, capsys):
    profile, ws = _profile(tmp_path)
    assert _file(profile) == 0
    assert "warning" not in capsys.readouterr().out
    assert (ws / "work/fix-the-thing/build/fix-the-thing-task.md").exists()


def test_an_unopenable_store_prints_one_warning_and_the_file_still_succeeds(tmp_path, store_at, capsys):
    # a state write that did not land is reported, never silent (#1197); the file is the source of truth either way
    profile, ws = _profile(tmp_path)
    store_at("nosuch://x")
    assert _file(profile) == 0
    warnings = [line for line in capsys.readouterr().out.splitlines() if line.startswith("warning:")]
    assert len(warnings) == 1 and "cannot open the store" in warnings[0]
    assert (ws / "work/fix-the-thing/build/fix-the-thing-task.md").exists()


def test_a_refused_mirror_prints_one_warning_and_the_file_still_succeeds(tmp_path, monkeypatch, capsys):
    profile, ws = _profile(tmp_path)
    monkeypatch.setattr("agent_tools.store_cli.set_state", lambda *a, **k: store_cli.StateRefused("nope"))
    assert _file(profile) == 0
    warnings = [line for line in capsys.readouterr().out.splitlines() if line.startswith("warning:")]
    assert len(warnings) == 1
    assert "fix-the-thing/fix-the-thing-task as ready" in warnings[0]
    assert (ws / "work/fix-the-thing/build/fix-the-thing-task.md").exists()


def test_an_intake_file_sets_no_state_and_mirrors_nothing(tmp_path, monkeypatch):
    profile, _ = _profile(tmp_path)
    seen = []
    monkeypatch.setattr("agent_tools.store_cli.mirror_state", lambda *args: seen.append(args))
    assert _file(profile, "--intake") == 0
    assert seen == []


def test_two_work_items_in_one_write_mirror_twice_and_one_warning_hides_nothing(tmp_path, monkeypatch, capsys):
    _, ws = _profile(tmp_path)
    seen = []

    def refuse(runs_dir, initiative, task, state, by, expected=None):
        seen.append((initiative, task, state))
        return store_cli.StateRefused("boom")

    monkeypatch.setattr("agent_tools.store_cli.set_state", refuse)
    mapping = {**route.initiative_files("One", "", "/r"), **route.initiative_files("Two", "", "/r")}
    assert cli._write_mapping(mapping, ws, by="me") is None
    assert seen == [("one", "one-task", "ready"), ("two", "two-task", "ready")]
    assert len([line for line in capsys.readouterr().out.splitlines() if line.startswith("warning:")]) == 2
