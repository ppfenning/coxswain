"""The lifecycle check arm and the `rescue` command run their checks under the lane worker cap."""

from __future__ import annotations

import os

import pytest

from harness import cli, rescue_checks
from harness.lane_workers import LANE_CAPACITY_VAR, XDIST_VAR, check_env
from harness.resume import load_result
from tests.test_cli_rescue import PHASE, RUN, TASK, _launch
from tests.test_epic_driver import cart, repo  # noqa: F401 -- fixtures

PRINT = f'echo "workers=[${XDIST_VAR}]"'

BOTH = pytest.mark.parametrize(
    ("capacity", "workers"), [("5", "4"), (None, "")], ids=["capacity-5-on-20-cores", "no-capacity"]
)


@pytest.fixture(autouse=True)
def twenty_cores(monkeypatch) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: 20)
    monkeypatch.delenv(LANE_CAPACITY_VAR, raising=False)
    monkeypatch.delenv(XDIST_VAR, raising=False)


def _capacity(monkeypatch, capacity: str | None) -> None:
    if capacity is not None:
        monkeypatch.setenv(LANE_CAPACITY_VAR, capacity)


def _states(result: dict, workers: str) -> bool:
    """The count a result carries as its workers field, absent when the child environment stated none."""
    return result.get("workers") == workers if workers else "workers" not in result


def _spy_rescue_results(monkeypatch) -> list[dict]:
    """Every result `rescue_checks` gets back from `run_checks`, which still runs for real."""
    seen: list[dict] = []
    real = rescue_checks.run_checks

    def spy(*args, **kwargs):
        results = real(*args, **kwargs)
        seen.extend(results)
        return results

    monkeypatch.setattr(rescue_checks, "run_checks", spy)
    return seen


@BOTH
def test_the_lifecycle_check_arm_prints_the_cap_and_states_it(tmp_path, monkeypatch, capacity, workers) -> None:
    _capacity(monkeypatch, capacity)
    (result,) = cli._run_worktree_checks(tmp_path, [{"name": "probe", "cmd": PRINT}], tmp_path / "logs")

    assert f"workers=[{workers}]" in result["output_tail"]
    assert _states(result, workers)


@BOTH
def test_the_rescue_command_checks_print_the_cap(monkeypatch, capsys, repo, cart, tmp_path, capacity, workers) -> None:  # noqa: F811
    _capacity(monkeypatch, capacity)
    cart["landing_areas"]["checks"] = [{"name": "probe", "cmd": PRINT}]
    results = _spy_rescue_results(monkeypatch)
    code, _, _, _ = _launch(monkeypatch, capsys, repo, cart, tmp_path)

    assert code == 0
    (row,) = load_result(tmp_path / "runs", RUN, PHASE, TASK)["evidence"]
    assert row["source"] == "harness_verify" and f"workers=[{workers}]" in row["output"]
    (result,) = results
    assert _states(result, workers)


def test_the_rescue_command_keeps_an_operator_set_count(monkeypatch, capsys, repo, cart, tmp_path) -> None:  # noqa: F811
    """The epic path passes no env when the operator already set the count, so the child inherits the operator's 7."""
    _capacity(monkeypatch, "5")
    monkeypatch.setenv(XDIST_VAR, "7")
    cart["landing_areas"]["checks"] = [{"name": "probe", "cmd": PRINT}]
    code, _, _, _ = _launch(monkeypatch, capsys, repo, cart, tmp_path)

    assert code == 0
    (row,) = load_result(tmp_path / "runs", RUN, PHASE, TASK)["evidence"]
    assert "workers=[7]" in row["output"]


def test_the_cli_env_is_what_check_env_builds_from_the_stated_capacity() -> None:
    environ = {"HOME": "/h", LANE_CAPACITY_VAR: " 5 "}
    assert cli._checks_env(environ, 20) == check_env(environ, 20, 5)
    assert cli._checks_env(environ, 20)[XDIST_VAR] == "4"


@pytest.mark.parametrize("stated", [None, "", "abc", "0", "-2"])
def test_a_missing_or_unusable_capacity_gives_no_environment(stated) -> None:
    environ = {"HOME": "/h"} if stated is None else {"HOME": "/h", LANE_CAPACITY_VAR: stated}
    assert cli._checks_env(environ, 20) is None
