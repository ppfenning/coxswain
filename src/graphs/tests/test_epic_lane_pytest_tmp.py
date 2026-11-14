"""Each epic check run gets its own pytest scratch directory under the run's directory, removed with the lane."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from test_epic_driver import cart, drive, initiative, repo  # noqa: F401 -- fixtures

import harness.epic as epic

RUN_ID = "epic-1"
PHASE = "p1-foundations"
TASKS = ("t1-probe", "t2-bench")
LEVEL = {"name": "level", "cmd": "true"}


@pytest.fixture(autouse=True)
def clean_pytest_environment(monkeypatch) -> None:
    """A lane runs this suite with its own --basetemp and TMPDIR exported; the cases below start from neither."""
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.delenv("TMPDIR", raising=False)


def _work() -> dict:
    work = initiative(two_phases=False)
    next(i for i in work["items"] if i["id"] == "t1-probe")["verify"] = ["echo verify"]
    return work


def _basetemp(env: dict) -> Path:
    flag = next(t for t in env["PYTEST_ADDOPTS"].split() if t.startswith("--basetemp="))
    return Path(flag.removeprefix("--basetemp="))


def _spy_checks(monkeypatch, parent: Path) -> list[tuple[str, dict, bool, set[str]]]:
    """Every `run_checks` call as (worktree name, env, basetemp existed, scratch names then present), still run for real."""
    calls: list[tuple[str, dict, bool, set[str]]] = []
    real = epic.run_checks

    def spy(worktree, checks, **kw):
        env = kw["env"]
        present = {p.name for p in parent.iterdir()} if parent.is_dir() else set()
        calls.append((worktree.name, env, _basetemp(env).is_dir(), present))
        return real(worktree, checks, **kw)

    monkeypatch.setattr(epic, "run_checks", spy)
    return calls


def _root(cart) -> Path:  # noqa: F811
    return Path(str(cart["landing_areas"]["worktree_root"])).expanduser()


def test_every_check_call_carries_a_basetemp_and_a_tmpdir_under_the_run_directory(
    repo, cart, tmp_path, monkeypatch  # noqa: F811
) -> None:
    cart["landing_areas"]["level_checks"] = [LEVEL]
    run_dir = _root(cart) / RUN_ID
    calls = _spy_checks(monkeypatch, run_dir / "pytest-tmp")
    drive(repo, cart, tmp_path, work=_work())
    assert calls
    for _, env, existed, _ in calls:
        assert _basetemp(env).is_relative_to(run_dir)
        assert Path(env["TMPDIR"]) == _basetemp(env)
        assert existed


def test_two_lanes_get_different_directories_and_one_lane_keeps_one(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    calls = _spy_checks(monkeypatch, _root(cart) / RUN_ID / "pytest-tmp")
    drive(repo, cart, tmp_path, work=_work())
    by_task = {task: {_basetemp(env) for name, env, _, _ in calls if name == task} for task in TASKS}
    assert all(len(dirs) == 1 for dirs in by_task.values())
    first, second = (next(iter(dirs)) for dirs in by_task.values())
    assert first != second


def test_the_phase_level_checks_use_a_directory_of_their_own(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    cart["landing_areas"]["level_checks"] = [LEVEL]
    parent = _root(cart) / RUN_ID / "pytest-tmp"
    calls = _spy_checks(monkeypatch, parent)
    drive(repo, cart, tmp_path, work=_work())
    lanes = {_basetemp(env) for name, env, _, _ in calls if name in TASKS}
    levels = {_basetemp(env) for name, env, _, _ in calls if name not in TASKS}
    assert levels and not levels & lanes
    assert {p.parent for p in levels | lanes} == {parent}


def test_a_name_a_task_could_carry_never_reaches_the_phase_level_directory() -> None:
    ctx = SimpleNamespace(worktree_root=Path("/runs"), run_id=RUN_ID)
    used = {epic._scratch_dir(ctx, PHASE, name) for name in ("@level", "@level-integration", "@trim", "@rescue-t1")}
    assert not used & {epic._scratch_dir(ctx, PHASE, name) for name in ("level", "level-integration", "trim", "t1")}


def test_a_lane_directory_is_gone_before_the_phase_level_checks_run(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    cart["landing_areas"]["level_checks"] = [LEVEL]
    calls = _spy_checks(monkeypatch, _root(cart) / RUN_ID / "pytest-tmp")
    drive(repo, cart, tmp_path, work=_work())
    at_level = [present for name, _, _, present in calls if name not in TASKS]
    assert at_level
    assert all(present == {f"{PHASE}--@level"} for present in at_level)


def test_nothing_is_left_behind_once_the_lanes_are_dropped(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    cart["landing_areas"]["level_checks"] = [LEVEL]
    calls = _spy_checks(monkeypatch, _root(cart) / RUN_ID / "pytest-tmp")
    drive(repo, cart, tmp_path, work=_work())
    assert calls
    assert not any(_basetemp(env).exists() for _, env, _, _ in calls)
    assert not (_root(cart) / RUN_ID / "pytest-tmp").exists()


def test_a_kept_run_does_not_carry_the_scratch_directory_as_a_phase(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    calls = _spy_checks(monkeypatch, _root(cart) / RUN_ID / "pytest-tmp")
    drive(repo, cart, tmp_path, work=_work(), keep_worktrees=True)
    kept = _root(cart) / "_kept" / RUN_ID
    assert calls
    assert kept.is_dir() and any(kept.iterdir())
    assert not (_root(cart) / RUN_ID / "pytest-tmp").exists()
    assert not any(kept.rglob("pytest-tmp"))


def test_an_operator_basetemp_is_kept_and_the_tmpdir_still_moves_under_the_run(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "--basetemp=/operator")
    ctx = SimpleNamespace(worktree_root=tmp_path, run_id=RUN_ID, lane_workers=None)
    env = epic._checks_env(ctx, {}, PHASE, "t1-probe")
    assert env["PYTEST_ADDOPTS"] == "--basetemp=/operator"
    assert Path(env["TMPDIR"]).is_relative_to(tmp_path / RUN_ID)
