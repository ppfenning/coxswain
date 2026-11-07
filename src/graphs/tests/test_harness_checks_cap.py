"""The review and rescue check paths run their checks under the lane worker cap, and only with a capacity."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import harness.cli as cli
from harness import rescue_checks
from harness.checks import run_checks
from harness.lane_workers import LANE_CAPACITY_VAR, XDIST_VAR

_ECHO = {"name": "echo", "cmd": f"echo W=${{{XDIST_VAR}-unset}}"}


class _Stop(Exception):
    """Raised by the stub that follows the check arm, so the test ends once the checks have run."""


@pytest.fixture
def host(monkeypatch):
    """A 20-core host whose environment states no worker count."""
    monkeypatch.delenv(XDIST_VAR, raising=False)
    monkeypatch.delenv(LANE_CAPACITY_VAR, raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 20)


def _spy_run_checks(monkeypatch, module) -> list[dict]:
    seen: list[dict] = []

    def spy(*args, **kwargs):
        results = run_checks(*args, **kwargs)
        seen.extend(results)
        return results

    monkeypatch.setattr(module, "run_checks", spy)
    return seen


def _rescue(tmp_path: Path, env) -> dict:
    return rescue_checks._verify_in(tmp_path, "patch", [_ECHO], lambda worktree, patch: None, env)


def _review(monkeypatch, tmp_path: Path) -> tuple[list[dict], str]:
    """Drive `_run_graph`'s check arm with a stub graph, stopping at the step after it."""
    result = {"build": {"patch": "p"}, "proposals": [{"kind": "draft_pr_create"}]}
    spec = SimpleNamespace(graph_name="lifecycle", run=lambda graph_args, runner: result)
    args = SimpleNamespace(
        graph="lifecycle",
        date="2026-11-06",
        repo=str(tmp_path),
        runs_dir=str(tmp_path / "runs"),
        worktree_root=str(tmp_path / "wt"),
        provider_profile="p",
        ledger=tmp_path / "ledger.jsonl",
    )
    cartridge = {"landing_areas": {"checks": [_ECHO]}}

    def create(repo, worktree, *, branch):
        worktree.mkdir(parents=True)
        return True, "ok"

    def stop(*a, **k):
        raise _Stop

    seen = _spy_run_checks(monkeypatch, cli)
    monkeypatch.setattr(cli, "_materialise", lambda *a: {})
    monkeypatch.setattr(cli, "_provider_profile_scope", lambda p: "p")
    monkeypatch.setattr(cli, "create_worktree", create)
    monkeypatch.setattr(cli, "apply_patch", lambda patch, worktree: (True, "ok"))
    monkeypatch.setattr(cli, "escalate_self_modification", stop)
    with pytest.raises(_Stop):
        cli._run_graph(specs={"lifecycle": spec}, parser=None, args=args, cartridge=cartridge, runner=None, run_id="r")
    return seen, (tmp_path / "runs" / "r" / "checks" / "1.log").read_text()


def test_the_checks_env_states_cores_over_capacity(monkeypatch, host) -> None:
    monkeypatch.setenv(LANE_CAPACITY_VAR, "5")
    assert cli._checks_env()[XDIST_VAR] == "4"


@pytest.mark.parametrize("capacity", [None, "", "0", "-2", "many"])
def test_the_checks_env_is_none_without_a_valid_capacity(monkeypatch, host, capacity) -> None:
    if capacity is not None:
        monkeypatch.setenv(LANE_CAPACITY_VAR, capacity)
    assert cli._checks_env() is None


def test_the_rescue_check_runs_under_the_cap(monkeypatch, tmp_path, host) -> None:
    monkeypatch.setenv(LANE_CAPACITY_VAR, "5")
    seen = _spy_run_checks(monkeypatch, rescue_checks)
    outcome = _rescue(tmp_path, cli._checks_env())
    assert "W=4" in outcome["rows"][0]["output"]
    assert seen[0]["workers"] == "4"


def test_the_rescue_check_without_a_capacity_leaves_the_variable_unset(monkeypatch, tmp_path, host) -> None:
    seen = _spy_run_checks(monkeypatch, rescue_checks)
    outcome = _rescue(tmp_path, cli._checks_env())
    assert "W=unset" in outcome["rows"][0]["output"]
    assert "workers" not in seen[0]


def test_the_review_check_runs_under_the_cap(monkeypatch, tmp_path, host) -> None:
    monkeypatch.setenv(LANE_CAPACITY_VAR, "5")
    seen, log = _review(monkeypatch, tmp_path)
    assert "W=4" in log
    assert seen[0]["workers"] == "4"


def test_the_review_check_without_a_capacity_leaves_the_variable_unset(monkeypatch, tmp_path, host) -> None:
    seen, log = _review(monkeypatch, tmp_path)
    assert "W=unset" in log
    assert "workers" not in seen[0]
