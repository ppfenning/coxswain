"""Epic lanes run their checks with a shared xdist worker count, taken from COX_LANE_CAPACITY and the core count."""

from __future__ import annotations

import os
import sys
from dataclasses import replace

import pytest
from test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    _DELETE_BENCH_TXT,
    _TRIM_PHASE,
    Runner,
    _trim_patches,
    _with_style,
    cart,
    drive,
    initiative,
    new_file_patch,
    repo,
)

import harness.epic as epic
from harness import check_reuse
from harness.lane_workers import LANE_CAPACITY_VAR, XDIST_VAR, lane_workers_from
from harness.resume import load_result
from tests.test_epic_rescue import PHASE, TASK, _rescue, _setup

LANE_DB = "LANE_DB"
LEVEL = {"name": "level", "cmd": "true"}
PRINT = f'echo "workers=[${XDIST_VAR}]"'


@pytest.fixture(autouse=True)
def twelve_cores(monkeypatch) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: 12)
    monkeypatch.delenv(LANE_CAPACITY_VAR, raising=False)
    monkeypatch.delenv(XDIST_VAR, raising=False)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)  # a lane exports its own --basetemp


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
    monkeypatch.setattr(check_reuse, "run_checks", spy)  # the task gate runs through the ledger
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


def test_no_capacity_adds_no_worker_count_but_every_environment_carries_the_lane_basetemp(
    repo, cart, tmp_path, monkeypatch  # noqa: F811
) -> None:
    cart["landing_areas"]["level_checks"] = [LEVEL]
    calls = _spy_checks(monkeypatch)
    drive(repo, cart, tmp_path, work=_work())
    assert calls and all("--basetemp=" in env["PYTEST_ADDOPTS"] and XDIST_VAR not in env for _, env in calls)


def test_an_operator_set_worker_count_is_kept(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setenv(LANE_CAPACITY_VAR, "3")
    monkeypatch.setenv(XDIST_VAR, "7")
    calls = _spy_checks(monkeypatch)
    drive(repo, cart, tmp_path, work=_work())
    assert calls and all(env[XDIST_VAR] == "7" for _, env in calls)


def test_workers_and_the_lane_database_travel_together(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setenv(LANE_CAPACITY_VAR, "3")
    cart["landing_areas"]["test_database"] = {"env": LANE_DB, "template": "postgresql://localhost/{lane}_test"}
    calls = _spy_checks(monkeypatch)
    result, _ = drive(repo, cart, tmp_path, work=_work())
    assert calls and all(env[XDIST_VAR] == "4" and env[LANE_DB].endswith("_test") for _, env in calls)
    assert all(t["test_database"].endswith("_test") for t in result["tasks"])


# ── end to end: a really spawned command prints what its environment holds ──────────────────────────────────────

BOTH = pytest.mark.parametrize(
    ("capacity", "workers"), [("5", "4"), (None, "")], ids=["capacity-5-on-20-cores", "no-capacity"]
)


def _twenty_cores(monkeypatch, capacity: str | None) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: 20)
    if capacity is not None:
        monkeypatch.setenv(LANE_CAPACITY_VAR, capacity)


def _states_workers(result: dict, workers: str) -> None:
    """The count a result carries as a field, absent when the child environment stated none."""
    assert (result.get("workers") == workers) if workers else ("workers" not in result)


@BOTH
def test_every_check_prints_the_cap_it_was_spawned_with(repo, cart, tmp_path, monkeypatch, capacity, workers) -> None:  # noqa: F811
    _twenty_cores(monkeypatch, capacity)
    fixed = tmp_path / "lint-fix.out"
    cart["landing_areas"]["checks"] = [{"name": "probe", "cmd": PRINT, "fix": f'echo "[${XDIST_VAR}]" > {fixed}'}]
    cart["landing_areas"]["level_checks"] = [{"name": "level", "cmd": PRINT}]
    work = _work()
    next(i for i in work["items"] if i["id"] == "t1-probe")["verify"] = [PRINT]
    result, _ = drive(repo, cart, tmp_path, work=work)

    printed = f"workers=[{workers}]"
    for task in result["tasks"]:
        (check,) = task["change_facts"]["checks"]
        assert printed in check["output_tail"]
        _states_workers(check, workers)
    evidence = {row["check"]: row["output"] for t in result["tasks"] for row in t["evidence"]}
    assert ("workers: 4" in evidence["checks:probe"]) == bool(workers)
    assert printed in evidence["verify:1"]
    assert ("workers: 4" in evidence["verify:1"]) == bool(workers)

    (level,) = result["phases"][0]["level_checks"]
    assert printed in level["output_tail"]
    _states_workers(level, workers)
    assert fixed.read_text().strip() == f"[{workers}]"


@BOTH
def test_the_coverage_floor_carries_the_cap(repo, cart, tmp_path, monkeypatch, capacity, workers) -> None:  # noqa: F811
    """The floor's checks run in a scratch worktree named `after`, and nothing captures their output but a file."""
    _twenty_cores(monkeypatch, capacity)
    seen = tmp_path / "seen.log"
    cmd = f'echo "$(pwd -P) [${XDIST_VAR}]" >> {seen}; {sys.executable} check.py'
    cart["landing_areas"]["checks"] = [{"name": "lint", "cmd": cmd}]
    runner = Runner(_trim_patches(), style={_TRIM_PHASE: _DELETE_BENCH_TXT})
    result, _ = drive(repo, _with_style(cart), tmp_path, runner=runner, work=initiative(two_phases=False))

    assert result["phases"][0]["trim"] == "trim: 1 lines removed"
    floor = [line for line in seen.read_text().splitlines() if line.split()[0].endswith("/after")]
    assert floor and all(line.endswith(f"[{workers}]") for line in floor)


@BOTH
def test_the_rescue_checks_carry_the_cap(repo, cart, tmp_path, monkeypatch, capacity, workers) -> None:  # noqa: F811
    _twenty_cores(monkeypatch, capacity)
    cart["landing_areas"]["checks"] = [{"name": "probe", "cmd": PRINT}]
    ctx, item, _ = _setup(repo, cart, tmp_path, new_file_patch("t1-probe.txt"))
    # `rescue_task` is not reached through `drive`; `run_epic` derives this field the same way.
    ctx = replace(ctx, lane_workers=lane_workers_from(os.environ, os.cpu_count() or 1))

    assert _rescue(ctx, item, Runner({}))["status"] == "approved"
    (row,) = load_result(ctx.runs_dir, "rescue-1", PHASE, TASK)["evidence"]
    assert row["source"] == "harness_verify" and f"workers=[{workers}]" in row["output"]
