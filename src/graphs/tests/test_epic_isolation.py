"""Driving an epic leaves the environment, the working directory and the epic modules as it found them."""

# ruff: noqa: F811  (cart and repo are imported fixtures that each test takes by name)

from __future__ import annotations

import copy
import os
from pathlib import Path
from types import ModuleType

import pytest

from harness import base_sync, epic
from tests.test_epic_base_check import DEPENDENT, NEED, _side_commit, _work
from tests.test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    CHECK_SCRIPT,
    Runner,
    cart,
    drive,
    git,
    new_file_patch,
    repo,
)
from tests.test_epic_multi_parent import phase_record, two_parent_work

MODULES = (epic, base_sync)


def _snapshot_value(value: object) -> object:
    """Containers are copied so an in-place fill shows up; anything else is kept for equality or identity."""
    if isinstance(value, (dict, list, set, bytearray)):
        return copy.deepcopy(value)
    return value


def module_state(*modules: ModuleType) -> dict[str, dict[str, object]]:
    return {
        mod.__name__: {k: _snapshot_value(v) for k, v in vars(mod).items() if not k.startswith("_")}
        for mod in modules
    }


def process_state() -> tuple[dict[str, str], str, dict[str, dict[str, object]]]:
    return dict(os.environ), os.getcwd(), module_state(*MODULES)


def _make_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=root)
    (root / "check.py").write_text(CHECK_SCRIPT, encoding="utf-8")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    git("add", "-A", cwd=root)
    git("commit", "-qm", "base", cwd=root)
    return root


def _world(base: Path, cart: dict) -> tuple[Path, dict]:
    """A repository and a cartridge whose worktrees live under `base`, so two worlds never share a path."""
    own = copy.deepcopy(cart)
    own["landing_areas"]["worktree_root"] = str(base / "worktrees")
    return _make_repo(base / "repo"), own


def _drive_base_check(repo: Path, cart: dict, base: Path) -> dict:
    commit = _side_commit(repo, "needed.txt", "ok\n")
    runner = Runner({DEPENDENT: new_file_patch(f"{DEPENDENT}.txt")})
    result, _ = drive(repo, cart, base, runner=runner, work=_work(commit))
    return result


def _drive_two_parent(repo: Path, cart: dict, base: Path) -> dict:
    result, _ = drive(repo, cart, base, work=two_parent_work())
    return result


@pytest.mark.usefixtures("at_task")
def test_driving_the_base_check_and_two_parent_scenarios_leaves_process_state_unchanged(repo, cart, tmp_path) -> None:
    before_env, before_cwd, before_modules = process_state()

    rebuilt = _drive_base_check(repo, cart, tmp_path)
    two_repo, two_cart = _world(tmp_path / "two", cart)
    joined = _drive_two_parent(two_repo, two_cart, tmp_path / "two")

    record = phase_record(rebuilt, "p2-rollout")
    assert record.get("base_rebuilt") == [NEED], record
    assert phase_record(joined, "p2-rollout")["assembled_from"] == ["p1-bench", "p1-foundations"]
    after_env, after_cwd, after_modules = process_state()
    assert after_env == before_env
    assert after_cwd == before_cwd
    assert after_modules == before_modules


@pytest.mark.usefixtures("at_task")
def test_the_same_scenario_driven_twice_in_two_directories_gives_the_same_result(cart, tmp_path) -> None:
    seen = []
    reasons = []
    for name in ("first", "second"):
        base = tmp_path / name
        base.mkdir()
        world_repo, world_cart = _world(base / "world", cart)
        result = _drive_base_check(world_repo, world_cart, base)
        seen.append(
            (
                [(p["phase"], p["status"]) for p in result["phases"]],
                {p["phase"]: p.get("base_rebuilt") for p in result["phases"]},
            )
        )
        reasons.append([(p["phase"], p.get("reason")) for p in result["phases"] if p["status"] != "complete"])

    assert seen[0] == seen[1], reasons
    assert seen[0][1]["p2-rollout"] == [NEED]
