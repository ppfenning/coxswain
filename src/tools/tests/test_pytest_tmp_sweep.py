import os
from pathlib import Path

from agent_tools.pytest_tmp_sweep import (
    DEFAULT_MAX_AGE_SECONDS,
    stale_run_dirs,
    sweep_pytest_tmp,
)

NOW = 1_000_000.0
HOUR = 3600.0


def test_default_limit_is_six_hours() -> None:
    assert DEFAULT_MAX_AGE_SECONDS == 6 * 60 * 60


def test_stale_run_dirs_returns_an_older_run() -> None:
    assert stale_run_dirs([("old", NOW - 7 * HOUR)], NOW) == ["old"]


def test_stale_run_dirs_keeps_a_newer_run() -> None:
    assert stale_run_dirs([("new", NOW - 1 * HOUR)], NOW) == []


def test_stale_run_dirs_keeps_a_run_exactly_at_the_limit() -> None:
    assert stale_run_dirs([("edge", NOW - 6 * HOUR)], NOW) == []


def test_stale_run_dirs_of_nothing_is_empty() -> None:
    assert stale_run_dirs([], NOW) == []


def test_stale_run_dirs_orders_oldest_first() -> None:
    entries = [("b", NOW - 8 * HOUR), ("a", NOW - 9 * HOUR), ("c", NOW - 1 * HOUR)]
    assert stale_run_dirs(entries, NOW) == ["a", "b"]


def _run(base: Path, name: str, file_age: float) -> Path:
    run = base / name
    run.mkdir(parents=True)
    f = run / "log.txt"
    f.write_text("x")
    os.utime(f, (NOW - file_age, NOW - file_age))
    os.utime(run, (NOW - file_age, NOW - file_age))
    return run


def test_sweep_removes_the_stale_run_and_keeps_the_fresh_one(tmp_path: Path) -> None:
    base = tmp_path / "pytest-of-alice"
    old = _run(base, "pytest-1", 7 * HOUR)
    new = _run(base, "pytest-2", 1 * HOUR)
    assert sweep_pytest_tmp(tmp_path, "alice", NOW) == ["pytest-1"]
    assert not old.exists()
    assert new.exists()


def test_sweep_ages_a_run_from_its_newest_file(tmp_path: Path) -> None:
    base = tmp_path / "pytest-of-alice"
    run = _run(base, "pytest-1", 7 * HOUR)
    fresh = run / "sub" / "live.txt"
    fresh.parent.mkdir()
    fresh.write_text("x")
    os.utime(fresh, (NOW - HOUR, NOW - HOUR))
    os.utime(fresh.parent, (NOW - HOUR, NOW - HOUR))
    assert sweep_pytest_tmp(tmp_path, "alice", NOW) == []
    assert run.exists()


def test_sweep_of_a_missing_user_directory_is_empty(tmp_path: Path) -> None:
    assert sweep_pytest_tmp(tmp_path, "nobody", NOW) == []
