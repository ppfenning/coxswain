import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_tools import chair_exec
from agent_tools.chair_exec import Deps, perform

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
HOUR = 3600.0


def _deps(temp_root: Path, recorded: list) -> Deps:
    return Deps(
        run=lambda argv: (0, ""),
        delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "",
        record=recorded.append,
        run_id=lambda action: "",
        repo_for=lambda action: "r",
        now=NOW.isoformat,
        temp_root=temp_root,
        user="pat",
    )


def _run_dir(temp_root: Path, name: str, age_hours: float) -> Path:
    path = temp_root / "pytest-of-pat" / name
    path.mkdir(parents=True)
    stamp = NOW.timestamp() - age_hours * HOUR
    os.utime(path, (stamp, stamp))
    return path


def _tick(deps: Deps) -> list:
    return perform([], deps, lambda: 1, dry_run=False)


def test_tick_removes_a_stale_run_directory_and_records_its_name(tmp_path: Path) -> None:
    stale = _run_dir(tmp_path, "pytest-1", age_hours=7)
    recorded: list = []
    results = _tick(_deps(tmp_path, recorded))
    assert not stale.exists()
    assert [r["reason"] for r in results] == ["removed stale pytest run directories: pytest-1"]
    assert [r["status"] for r in recorded] == ["done"]


def test_tick_keeps_a_fresh_run_directory_and_says_nothing(tmp_path: Path) -> None:
    fresh = _run_dir(tmp_path, "pytest-2", age_hours=1)
    recorded: list = []
    results = _tick(_deps(tmp_path, recorded))
    assert fresh.exists()
    assert results == []
    assert recorded == []


def test_tick_stays_successful_when_the_sweep_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kept = _run_dir(tmp_path, "pytest-3", age_hours=7)

    def boom(*args: object) -> list[str]:
        raise RuntimeError("sweep broke")

    monkeypatch.setattr(chair_exec, "sweep_pytest_tmp", boom)
    recorded: list = []
    action = {"kind": "take_lease", "epoch": 1}
    results = perform([action], _deps(tmp_path, recorded), lambda: 1, dry_run=False)
    assert kept.exists()
    assert [r["action"]["kind"] for r in results] == ["take_lease"]
    assert all(r["status"] != "failed" for r in results)


def test_sweep_row_follows_a_refused_actions_result(tmp_path: Path) -> None:
    _run_dir(tmp_path, "pytest-5", age_hours=7)
    deps = replace(_deps(tmp_path, []), acquire_lease=lambda holder, host: "held by another")
    results = perform([{"kind": "take_lease", "epoch": 1}], deps, lambda: 1, dry_run=False)
    assert [r["action"]["kind"] for r in results] == ["take_lease", "pytest_tmp_sweep"]
    assert results[0]["status"] != "done"


def test_dry_run_sweeps_nothing(tmp_path: Path) -> None:
    stale = _run_dir(tmp_path, "pytest-4", age_hours=7)
    assert perform([], _deps(tmp_path, []), lambda: 1, dry_run=True) == []
    assert stale.exists()
