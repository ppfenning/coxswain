"""The land's checks carry the lane cap: on 20 cores and capacity 5 a check sees 4 xdist workers."""
from __future__ import annotations

from pathlib import Path

from agent_tools import cli

PRINT_WORKERS = 'printf "%s" "$PYTEST_XDIST_AUTO_NUM_WORKERS" > out.txt'


def test_land_check_sees_four_workers_and_the_detail_names_them(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)
    monkeypatch.setenv("COX_LANE_CAPACITY", "5")
    monkeypatch.setattr(cli.os, "cpu_count", lambda: 20)
    capacity = cli._check_capacity(tmp_path, {}, "host")
    ok, detail = cli._run_checks([("tests", PRINT_WORKERS)], tmp_path, cli.os.cpu_count(), capacity)
    assert (ok, detail) == (True, "1 checks passed (xdist workers: 4)")
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "4"


def test_a_venv_worktree_keeps_its_path_prefix_beside_the_variable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    env = cli._checks_env(tmp_path, 20, 5)
    assert env is not None
    assert env["PATH"].startswith(f"{tmp_path / '.venv' / 'bin'}{cli.os.pathsep}")
    assert env["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "4"
    assert cli._run_checks([("tests", PRINT_WORKERS)], tmp_path, 20, 5) == (True, "1 checks passed (xdist workers: 4)")


def test_no_capacity_leaves_the_environment_and_the_detail_alone(tmp_path: Path) -> None:
    assert cli._checks_env(tmp_path, 20, None) is None
    assert cli._run_checks([("tests", "true")], tmp_path, 20, None) == (True, "1 checks passed")


def test_capacity_comes_from_the_variable_when_set_else_the_host_cap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_local_max_in_flight", lambda runs_dir, profile, host: 7)
    monkeypatch.delenv("COX_LANE_CAPACITY", raising=False)
    assert cli._check_capacity(tmp_path, {}, "host") == 7
    monkeypatch.setenv("COX_LANE_CAPACITY", "5")
    assert cli._check_capacity(tmp_path, {}, "host") == 5
    monkeypatch.setenv("COX_LANE_CAPACITY", "")
    assert cli._check_capacity(tmp_path, {}, "host") is None


def test_an_applied_land_stamps_cores_and_capacity_on_checks_steps_only() -> None:
    steps = [{"kind": "checks", "checks": []}, {"kind": "push", "branch": "b"}]
    assert cli._with_check_cap(steps, 20, 5) == [{"kind": "checks", "checks": [], "cores": 20, "capacity": 5}, steps[1]]
