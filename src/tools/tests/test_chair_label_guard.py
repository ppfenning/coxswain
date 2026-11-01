from __future__ import annotations

import datetime
import json
import os

import pytest

from agent_tools import chair_exec, cli, leader

ACTIONS = [
    {"kind": "land", "run": "r-1", "task_id": "t1", "repo": "r"},
    {"kind": "land_phase", "run": "r-1", "phase": "p1", "repo": "r"},
    {"kind": "rescue", "initiative": "x", "task_id": "t1"},
    {"kind": "launch_epic", "initiative": "x", "repo": "/r", "host": "h"},
    {"kind": "launch_decompose", "intake_ids": ["intake/x.md"]},
]
IDS = ["land", "land_phase", "rescue", "launch_epic", "launch_decompose"]


def _live_lock(tmp_path, monkeypatch) -> None:
    for name in ("COX_SESSION_LABEL", "CLAUDE_PID", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(leader, "pid_alive", lambda pid: True)
    monkeypatch.setattr(cli, "_leader_identity", lambda: (os.getpid(), "h"))
    now = datetime.datetime.now(datetime.UTC).isoformat()
    record = {"session": "chair-jarvis", "pid": os.getpid(), "host": "h", "taken_at": now, "heartbeat_at": now}
    (tmp_path / "leader.json").write_text(json.dumps(record))


def _guard_as_the_command_does(tmp_path, argv: list[str]) -> int | None:
    """Parse the chair's argv with the real parser, then make the guard call cli.py makes at its land and launch entry points."""
    a = cli.build_parser().parse_args(argv[1:])
    return cli._leader_guard_or_refuse(tmp_path, cli._holder_label(a), a.force, claim=not a.no_claim, explicit_label=getattr(a, "label", None))


@pytest.mark.parametrize("action", ACTIONS, ids=IDS)
def test_the_chair_s_labelled_argv_passes_the_real_guard_over_its_own_live_lock(tmp_path, monkeypatch, action) -> None:
    _live_lock(tmp_path, monkeypatch)
    assert _guard_as_the_command_does(tmp_path, chair_exec.argv_for(action, "x-id", label="chair-jarvis")) is None


@pytest.mark.parametrize("action", ACTIONS, ids=IDS)
def test_the_chair_s_unlabelled_argv_is_refused_by_the_real_guard_over_its_own_live_lock(tmp_path, monkeypatch, capsys, action) -> None:
    _live_lock(tmp_path, monkeypatch)
    assert _guard_as_the_command_does(tmp_path, chair_exec.argv_for(action, "x-id")) == 2
    assert "held by chair-jarvis (live)" in capsys.readouterr().out


def test_a_chair_with_no_label_passes_none_rather_than_the_unlabeled_sentinel(tmp_path) -> None:
    deps = cli._chair_run_deps(tmp_path, {}, cli._UNLABELED, 1, "h", False, print, tmp_path / "p.yaml", "files")
    assert deps.exec_deps.label == ""


def test_the_chair_run_bundle_hands_its_session_label_to_the_exec_deps(tmp_path) -> None:
    deps = cli._chair_run_deps(tmp_path, {}, "chair-jarvis", 1, "h", False, print, tmp_path / "p.yaml", "files")
    assert deps.exec_deps.label == "chair-jarvis"
    land = {"kind": "land_phase", "run": "r-1", "phase": "p1", "repo": "r"}
    assert chair_exec.argv_for(land, label=deps.exec_deps.label)[-2:] == ["--label", "chair-jarvis"]
