"""Each epic check run gets its own temp directory, under the run's scratch root in the system temp directory, removed
with the lane. None is under the run's worktree directory, and no check is handed a pytest option of the harness's."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    Runner,
    cart,
    drive,
    initiative,
    new_file_patch,
    repo,
)
from test_epic_rescue import _rescue, _setup

import harness.epic as epic
from harness.lane_pytest_tmp import lane_scratch_dir, run_scratch_root

RUN_ID = "epic-1"
PHASE = "p1-foundations"
TASKS = ("t1-probe", "t2-bench")
LEVEL = {"name": "level", "cmd": "true"}


@pytest.fixture(autouse=True)
def clean_pytest_environment(monkeypatch) -> None:
    """A lane runs this suite with its own TMPDIR exported (an older harness also a --basetemp); the cases start from neither."""
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.delenv("TMPDIR", raising=False)


def _work() -> dict:
    work = initiative(two_phases=False)
    next(i for i in work["items"] if i["id"] == "t1-probe")["verify"] = ["echo verify"]
    return work


def _tmpdir(env: dict) -> Path:
    return Path(env["TMPDIR"])


def _spy_checks(monkeypatch, parent: Path) -> list[tuple[str, dict, bool, set[str]]]:
    """Every `run_checks` call as (worktree name, env, TMPDIR existed, scratch names then present), still run for real."""
    calls: list[tuple[str, dict, bool, set[str]]] = []
    real = epic.run_checks

    def spy(worktree, checks, **kw):
        env = kw["env"]
        present = {p.name for p in parent.iterdir()} if parent.is_dir() else set()
        calls.append((worktree.name, env, _tmpdir(env).is_dir(), present))
        return real(worktree, checks, **kw)

    monkeypatch.setattr(epic, "run_checks", spy)
    return calls


def _root(cart) -> Path:  # noqa: F811
    return Path(str(cart["landing_areas"]["worktree_root"])).expanduser()


def _scratch_parent(cart) -> Path:  # noqa: F811
    """Where the run's lane directories sit: the run's scratch root, under the system temp directory."""
    return run_scratch_root(Path(tempfile.gettempdir()), _root(cart) / RUN_ID) / "pytest-tmp"


def test_every_check_call_carries_a_tmpdir_of_the_runs_own_outside_its_worktrees_and_no_pytest_option(
    repo, cart, tmp_path, monkeypatch  # noqa: F811
) -> None:
    cart["landing_areas"]["level_checks"] = [LEVEL]
    calls = _spy_checks(monkeypatch, _scratch_parent(cart))
    drive(repo, cart, tmp_path, work=_work())
    assert calls
    for _, env, existed, _ in calls:
        assert _tmpdir(env).parent == _scratch_parent(cart)
        assert _tmpdir(env).is_relative_to(Path(tempfile.gettempdir()))
        assert not _tmpdir(env).is_relative_to(_root(cart))
        assert "PYTEST_ADDOPTS" not in env
        assert existed


def test_two_lanes_get_different_directories_and_one_lane_keeps_one(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    calls = _spy_checks(monkeypatch, _scratch_parent(cart))
    drive(repo, cart, tmp_path, work=_work())
    by_task = {task: {_tmpdir(env) for name, env, _, _ in calls if name == task} for task in TASKS}
    assert all(len(dirs) == 1 for dirs in by_task.values())
    first, second = (next(iter(dirs)) for dirs in by_task.values())
    assert first != second


def test_the_phase_level_checks_use_a_directory_of_their_own(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    cart["landing_areas"]["level_checks"] = [LEVEL]
    parent = _scratch_parent(cart)
    calls = _spy_checks(monkeypatch, parent)
    drive(repo, cart, tmp_path, work=_work())
    lanes = {_tmpdir(env) for name, env, _, _ in calls if name in TASKS}
    levels = {_tmpdir(env) for name, env, _, _ in calls if name not in TASKS}
    assert levels and not levels & lanes
    assert {p.parent for p in levels | lanes} == {parent}


def test_a_name_a_task_could_carry_never_reaches_the_phase_level_directory() -> None:
    ctx = SimpleNamespace(worktree_root=Path("/runs"), run_id=RUN_ID)
    used = {epic.scratch_dir(ctx, PHASE, name) for name in ("@level", "@level-integration", "@trim", "@rescue-t1")}
    assert not used & {epic.scratch_dir(ctx, PHASE, name) for name in ("level", "level-integration", "trim", "t1")}


def test_two_runs_of_one_id_under_two_worktree_roots_never_share_a_directory() -> None:
    one = SimpleNamespace(worktree_root=Path("/a/worktrees"), run_id=RUN_ID)
    other = SimpleNamespace(worktree_root=Path("/b/worktrees"), run_id=RUN_ID)
    assert epic.scratch_dir(one, PHASE, "t1") != epic.scratch_dir(other, PHASE, "t1")


def test_a_lane_directory_is_gone_before_the_phase_level_checks_run(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    cart["landing_areas"]["level_checks"] = [LEVEL]
    calls = _spy_checks(monkeypatch, _scratch_parent(cart))
    drive(repo, cart, tmp_path, work=_work())
    at_level = [present for name, _, _, present in calls if name not in TASKS]
    assert at_level
    assert all(present == {lane_scratch_dir(Path("/r"), PHASE, "@level").name} for present in at_level)


def test_nothing_is_left_behind_once_the_lanes_are_dropped(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    cart["landing_areas"]["level_checks"] = [LEVEL]
    calls = _spy_checks(monkeypatch, _scratch_parent(cart))
    drive(repo, cart, tmp_path, work=_work())
    assert calls
    assert not any(_tmpdir(env).exists() for _, env, _, _ in calls)
    assert not _scratch_parent(cart).parent.exists()
    assert not (_root(cart) / RUN_ID / "pytest-tmp").exists()


def test_a_kept_run_does_not_carry_the_scratch_directory_as_a_phase(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    calls = _spy_checks(monkeypatch, _scratch_parent(cart))
    drive(repo, cart, tmp_path, work=_work(), keep_worktrees=True)
    kept = _root(cart) / "_kept" / RUN_ID
    assert calls
    assert kept.is_dir() and any(kept.iterdir())
    assert not _scratch_parent(cart).parent.exists()
    assert not (_root(cart) / RUN_ID / "pytest-tmp").exists()
    assert not any(kept.rglob("pytest-tmp"))


def test_an_operators_pytest_addopts_pass_through_and_the_tmpdir_still_moves_to_the_runs_own(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "temp"))
    monkeypatch.setenv("PYTEST_ADDOPTS", "--basetemp=/operator")
    ctx = SimpleNamespace(worktree_root=tmp_path / "worktrees", run_id=RUN_ID, lane_workers=None)
    env = epic.checks_env(ctx, {}, PHASE, "t1-probe")
    assert env["PYTEST_ADDOPTS"] == "--basetemp=/operator"
    assert _tmpdir(env).parent == epic.run_scratch(ctx) / "pytest-tmp"
    assert _tmpdir(env).is_relative_to(tmp_path / "temp") and _tmpdir(env).is_dir()


def test_a_rescue_removes_its_runs_temp_directory(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    ctx, item, _ = _setup(repo, cart, tmp_path, new_file_patch("t1-probe.txt"))
    seen: list[Path] = []
    real = epic.checks_env

    def spy(ctx, lane, phase, task):
        env = real(ctx, lane, phase, task)
        seen.append(_tmpdir(env))
        return env

    monkeypatch.setattr(epic, "checks_env", spy)
    assert _rescue(ctx, item, Runner({}))["status"] == "approved"
    assert seen and not any(path.exists() for path in seen)
    assert not epic.run_scratch(ctx).exists()
