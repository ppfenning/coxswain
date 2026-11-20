"""With the workspace out of git, a command that reads the board refreshes this machine's files from the store first."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from agent_tools import cli, queue_export, run_store, store_fill


def test_only_commands_that_read_the_board_files_refresh_them() -> None:
    assert cli.reads_board(["route", "status"])
    assert cli.reads_board(["lander", "status"])
    assert not cli.reads_board(["chair", "tick"]), "the tick exports for itself"
    assert not cli.reads_board(["route", "export"]), "the export is the refresh"
    assert not cli.reads_board(["route", "import", "--workspace", "x"])
    assert not cli.reads_board(["host", "list"])
    assert not cli.reads_board([])


def _files(**texts: str) -> list[tuple[tuple[str, ...], str]]:
    return [(tuple(path.split("/")), text) for path, text in texts.items()]


def test_an_unchanged_exported_copy_of_a_dropped_row_is_stale_and_a_new_or_edited_file_is_not() -> None:
    task = "---\nid: a\nphase: p1\nstate: ready\ntitle: A\n---\nbody\n"
    edited = task.replace("body", "edited here")
    intake = "---\nid: fresh\ntitle: Fresh\n---\nidea\n"
    files = _files(**{"work/gone/p1/a.md": task, "work/gone/p1/b.md": edited.replace("id: a", "id: b"), "intake/fresh.md": intake,
                      "work/kept/p1/a.md": task})
    record = {"work/gone/p1/a.md": queue_export.blob_hash(task), "work/gone/p1/b.md": queue_export.blob_hash(task),
              "work/kept/p1/a.md": queue_export.blob_hash(task)}
    existing = [{"initiative": "kept", "task_id": "a", "kind": "task"}]
    assert store_fill.stale_exports(files, existing, record, queue_export.blob_hash) == ["work/gone/p1/a.md"]


@pytest.fixture
def out_of_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list]:
    ws = tmp_path / "ws"
    (ws / "runs").mkdir(parents=True)
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {ws}\nchair:\n  workspace_git: false\n", encoding="utf-8")
    exports: list = []
    monkeypatch.setattr(cli, "_export_board", lambda runs_dir, workspace, prune_stale=False: exports.append((workspace, prune_stale)) or [])
    return profile, exports


def test_the_files_are_rewritten_once_per_store_version(out_of_git, monkeypatch: pytest.MonkeyPatch) -> None:
    profile, exports = out_of_git
    stamp = [(3, 111)]
    monkeypatch.setattr(run_store, "queue_stamp", lambda runs_dir: stamp[0])
    a = argparse.Namespace(profile=str(profile))
    cli._refresh_board_files(a)
    cli._refresh_board_files(a)
    assert len(exports) == 1 and exports[0][1] is True, "one export for one store version, pruning stale copies"
    stamp[0] = (4, 222)
    cli._refresh_board_files(a)
    assert len(exports) == 2
    assert json.loads((exports[0][0] / "runs" / ".export-stamp").read_text()) == [4, 222]


def test_a_store_that_cannot_answer_or_an_export_that_fails_never_stops_the_command(out_of_git, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    profile, exports = out_of_git
    a = argparse.Namespace(profile=str(profile))
    monkeypatch.setattr(run_store, "queue_stamp", lambda runs_dir: None)
    cli._refresh_board_files(a)
    assert exports == []

    def boom(*args, **kwargs):
        raise RuntimeError("store-fill: the store could not be read; nothing written")

    monkeypatch.setattr(run_store, "queue_stamp", lambda runs_dir: (1, 1))
    monkeypatch.setattr(cli, "_export_board", boom)
    cli._refresh_board_files(a)
    assert "could not refresh" in capsys.readouterr().err


def test_a_workspace_kept_in_git_is_never_refreshed(tmp_path: Path, out_of_git, monkeypatch: pytest.MonkeyPatch) -> None:
    profile, exports = out_of_git
    profile.write_text(profile.read_text().replace("workspace_git: false", "workspace_git: true"), encoding="utf-8")
    monkeypatch.setattr(run_store, "queue_stamp", lambda runs_dir: (1, 1))
    cli._refresh_board_files(argparse.Namespace(profile=str(profile)))
    assert exports == []


def test_a_command_with_no_profile_flag_is_refreshed_from_the_profile_in_the_environment(out_of_git, monkeypatch: pytest.MonkeyPatch) -> None:
    """`route lint` and others declare no --profile; the refresh must not need the attribute."""
    profile, exports = out_of_git
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(profile))
    monkeypatch.setattr(run_store, "queue_stamp", lambda runs_dir: (2, 7))
    cli._refresh_board_files(argparse.Namespace(initiative_dir="work/demo"))
    assert len(exports) == 1
