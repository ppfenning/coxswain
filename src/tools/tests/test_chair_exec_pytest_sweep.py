import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_tools import chair_exec
from agent_tools.chair_exec import Deps, perform
from agent_tools.store_url import TracesRoot

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
USER = "pat"
HOUR = 3600


def _run_dir(temp_root: Path, name: str, age_s: float) -> Path:
    path = temp_root / f"pytest-of-{USER}" / name
    path.mkdir(parents=True)
    (path / "test_one0").mkdir()
    stamp = NOW.timestamp() - age_s
    for item in (path / "test_one0", path):
        os.utime(item, (stamp, stamp))
    return path


def _tick(
    monkeypatch: pytest.MonkeyPatch, temp_root: Path | None, calls: list | None = None, now: str = NOW.isoformat()
) -> dict:
    def run(argv: list) -> tuple[int, str]:
        if calls is not None:
            calls.append(argv)
        return (0, "ok")

    monkeypatch.setattr(chair_exec.run_store, "_traces_root", lambda runs_dir: TracesRoot("t/traces", False))
    deps = Deps(
        run=run, delete_branches=lambda repo, pattern: ([], ""), acquire_lease=lambda holder, host: "",
        record=lambda action: None, run_id=lambda action: "", repo_for=lambda action: "",
        now=lambda: now, temp_root=temp_root, user=USER,
    )
    return perform([{"kind": "housekeeping", "epoch": 1}], deps, lambda: 1, False)[0]


@pytest.fixture
def new_york(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_a_tick_removes_a_stale_run_directory_and_names_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stale = _run_dir(tmp_path, "pytest-3", age_s=7 * HOUR)
    result = _tick(monkeypatch, tmp_path)
    assert not stale.exists()
    assert result["status"] == "done"
    assert result["reason"].endswith("; pytest tmp: removed pytest-3")


def test_a_tick_keeps_a_fresh_run_directory_and_says_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fresh = _run_dir(tmp_path, "pytest-4", age_s=HOUR)
    result = _tick(monkeypatch, tmp_path)
    assert fresh.is_dir()
    assert result["reason"] == _tick(monkeypatch, None)["reason"]


def test_an_exception_from_the_sweep_leaves_the_tick_successful(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> list[str]:
        raise RuntimeError("disk gone")

    monkeypatch.setattr(chair_exec, "sweep_pytest_tmp", boom)
    calls: list = []
    result = _tick(monkeypatch, tmp_path, calls)
    assert result["status"] == "done"
    assert result["reason"] == _tick(monkeypatch, None)["reason"]
    assert ["cox", "lake", "sync"] in calls


@pytest.mark.usefixtures("new_york")
def test_a_clock_with_no_offset_is_read_as_utc_so_a_live_run_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    live = _run_dir(tmp_path, "pytest-5", age_s=2 * HOUR)
    _tick(monkeypatch, tmp_path, now=NOW.replace(tzinfo=None).isoformat())
    assert live.is_dir()
