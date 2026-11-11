"""The applied worker count rides on check results and evidence, only when the env states one."""

from __future__ import annotations

import sys

import pytest

from harness.checks import checks_evidence, run_checks
from harness.lane_workers import XDIST_VAR

_OK = {"name": "ok", "cmd": f"{sys.executable} -c 'print(1)'"}
_BAD = {"name": "bad", "cmd": f"{sys.executable} -c 'raise SystemExit(1)'"}
_SLOW = {"name": "slow", "cmd": f"{sys.executable} -c 'import time; time.sleep(30)'", "timeout": 1}
_BASE = {"PATH": "/usr/bin:/bin"}


@pytest.fixture(autouse=True, params=[None, "1", "64"], ids=["ambient-unset", "ambient-1", "ambient-64"])
def ambient_workers(request, monkeypatch) -> None:
    """The variable pytest-xdist sets in a worker, established here; monkeypatch restores it."""
    if request.param is None:
        monkeypatch.delenv(XDIST_VAR, raising=False)
    else:
        monkeypatch.setenv(XDIST_VAR, request.param)


def test_a_passing_check_carries_the_workers_and_an_evidence_line(tmp_path):
    results = run_checks(tmp_path, [_OK], env={**_BASE, XDIST_VAR: "4"})
    assert results[0]["workers"] == "4"
    assert checks_evidence(results)[0]["output"].splitlines()[-1] == "workers: 4"


def test_every_outcome_carries_the_workers(tmp_path):
    env = {**_BASE, XDIST_VAR: "4"}
    failed = run_checks(tmp_path, [_BAD], env=env)
    timed_out = run_checks(tmp_path, [_SLOW], env=env)
    unrunnable = run_checks(tmp_path / "missing", [_OK], env=env)
    assert [r["outcome"] for r in (*failed, *timed_out, *unrunnable)] == ["failed", "timed_out", "unrunnable"]
    for result in (*failed, *timed_out, *unrunnable):
        assert result["workers"] == "4"
        assert "workers: 4" in checks_evidence([result])[0]["output"].splitlines()


def test_without_the_variable_results_and_evidence_carry_no_workers(tmp_path):
    results = run_checks(tmp_path, [_OK, _BAD], env=_BASE)
    assert all("workers" not in r for r in results)
    assert all("workers" not in row["output"] for row in checks_evidence(results))


def test_an_inherited_env_carries_no_workers(tmp_path, monkeypatch):
    monkeypatch.setenv(XDIST_VAR, "64")
    assert "workers" not in run_checks(tmp_path, [_OK])[0]
