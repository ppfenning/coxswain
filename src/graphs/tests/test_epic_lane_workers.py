"""Epic lanes run their checks with a shared xdist worker count, taken from COX_LANE_CAPACITY and the core count."""

from __future__ import annotations

import os

import pytest
from test_epic_driver import cart, drive, initiative, repo  # noqa: F401 -- fixtures

import harness.epic as epic
from harness.lane_workers import LANE_CAPACITY_VAR, XDIST_VAR

LANE_DB = "LANE_DB"
LEVEL = {"name": "level", "cmd": "true"}


@pytest.fixture(autouse=True)
def twelve_cores(monkeypatch) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: 12)
    monkeypatch.delenv(LANE_CAPACITY_VAR, raising=False)
    monkeypatch.delenv(XDIST_VAR, raising=False)


def _work() -> dict:
    work = initiative(two_phases=False)
    next(i for i in work["items"] if i["id"] == "t1-probe")["verify"] = ["echo verify"]
    return work


def _spy_checks(monkeypatch) -> list[tuple[bool, dict | None]]:
    """Every `run_checks` call as (is a level check, env), still run for real."""
    calls: list[tuple[bool, dict | None]] = []
    real = epic.run_checks

    def spy(worktree, checks, **kw):
        calls.append((str(kw.get("log_prefix", "")).endswith("/level/"), kw.get("env")))
        return real(worktree, checks, **kw)

    monkeypatch.setattr(epic, "run_checks", spy)
    return calls


def test_capacity_3_on_12_cores_gives_4_workers_to_every_task_call_and_the_phase_check(
    repo, cart, tmp_path, monkeypatch  # noqa: F811
) -> None:
    monkeypatch.setenv(LANE_CAPACITY_VAR, "3")
    cart["landing_areas"]["level_checks"] = [LEVEL]
    calls = _spy_checks(monkeypatch)
    result, _ = drive(repo, cart, tmp_path, work=_work())
    assert any(level for level, _ in calls) and any(not level for level, _ in calls)
    assert all(env[XDIST_VAR] == "4" for _, env in calls)
    assert not any("test_database" in t for t in result["tasks"])


def test_no_capacity_leaves_every_environment_as_today(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    cart["landing_areas"]["level_checks"] = [LEVEL]
    calls = _spy_checks(monkeypatch)
    drive(repo, cart, tmp_path, work=_work())
    assert calls and all(env is None for _, env in calls)


def test_an_operator_set_worker_count_is_kept(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setenv(LANE_CAPACITY_VAR, "3")
    monkeypatch.setenv(XDIST_VAR, "7")
    calls = _spy_checks(monkeypatch)
    drive(repo, cart, tmp_path, work=_work())
    assert calls and all(env is None or env[XDIST_VAR] == "7" for _, env in calls)


def test_workers_and_the_lane_database_travel_together(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setenv(LANE_CAPACITY_VAR, "3")
    cart["landing_areas"]["test_database"] = {"env": LANE_DB, "template": "postgresql://localhost/{lane}_test"}
    calls = _spy_checks(monkeypatch)
    result, _ = drive(repo, cart, tmp_path, work=_work())
    assert calls and all(env[XDIST_VAR] == "4" and env[LANE_DB].endswith("_test") for _, env in calls)
    assert all(t["test_database"].endswith("_test") for t in result["tasks"])
