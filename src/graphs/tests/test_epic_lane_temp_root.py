"""A lane runs a repository's suite with the temp directory a hand run gets: outside the home directory, and with no
pytest option of the harness's own.

Lane hosts keep their worktrees under the home directory (`~/worktrees`). With each lane's temp files under the run's
worktree directory, a suite that reads a path under a home directory as a home path (a recorded repo path is read that
way) failed in every lane and passed by hand. With `--basetemp` exported in PYTEST_ADDOPTS, a pytest the suite starts
itself inherited it and refused to run below it. The fake suite below does both things the coxswain suites do.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from test_epic_driver import cart, drive, git, initiative, repo  # noqa: F401 -- cart and repo are fixtures

from harness import cli
from harness.checks import run_checks

SUITE = '''\
import os
import subprocess
import sys
from pathlib import Path


def test_a_temp_path_is_not_under_the_home_directory(tmp_path):
    """Code that reads a path under a home directory as a home path finds no temp path to misread."""
    assert not tmp_path.resolve().is_relative_to(Path(os.environ["HOME"]).resolve())


def test_a_pytest_the_suite_starts_runs(tmp_path):
    (tmp_path / "test_inner.py").write_text("def test_inner(tmp_path):\\n    assert tmp_path.is_dir()\\n")
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_inner.py"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert tmp_path.is_dir()
'''
CHECK = {"name": "suite", "cmd": f"{sys.executable} -m pytest -q -p no:cacheprovider tests"}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch) -> Path:
    """The home directory the harness runs under, as on a lane host."""
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)  # a lane started by an older harness exports its own --basetemp
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    return root


def _write_suite(root: Path) -> Path:
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_suite.py").write_text(SUITE, encoding="utf-8")
    return root


def test_the_suite_passes_run_by_hand(tmp_path) -> None:
    """The control: the same command in the same tree, with this process's own environment."""
    [result] = run_checks(_write_suite(tmp_path / "tree"), [CHECK])
    assert result["passed"], result["output_tail"]
    assert result["counts"] == {"passed": 2}


def test_an_epic_lane_runs_the_suite_as_a_hand_run_does(repo, cart, tmp_path, home) -> None:  # noqa: F811
    _write_suite(repo)
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "suite", cwd=repo)
    cart["landing_areas"]["worktree_root"] = str(home / "worktrees")
    cart["landing_areas"]["checks"] = [CHECK]

    result, _ = drive(repo, cart, tmp_path, work=initiative(two_phases=False))

    assert not result["quarantined"], [q["reason"] for q in result["quarantined"]]
    assert result["totals"]["phases_complete"] == 1


def test_a_lifecycle_worktree_check_runs_the_suite_as_a_hand_run_does(tmp_path, home) -> None:
    worktree = _write_suite(tmp_path / "wt")
    [result] = cli._run_worktree_checks(worktree, [CHECK], home / "workspace" / "runs" / "run-1" / "checks")
    assert result["passed"], result["output_tail"]
    assert result["counts"] == {"passed": 2}
