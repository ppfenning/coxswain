"""The tree a lane's builder reads, against real git (E1, 2026-11-21). A patch-returning node cuts its scratch at its
runner's `repo_dir` HEAD; the lane's runner is pointed at the shared checkout, whose HEAD is the default branch, so a
revision of files the phase itself added could not be built there (`patch_outside_scratch`). `phase_lane.builder_runner`
hands each build a view of the runner reading a tree at the task's base. The scripted runner's tree mode
(`runner.scripted.TREE`) builds the way the Claude Code runner does, which is what lets this be seen offline."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness import phase_lane
from runner.protocol import RunnerError
from runner.scripted import TREE, ScriptedRunner


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, name: str, text: str) -> str:
    (repo / name).write_text(text, encoding="utf-8")
    _git("add", name, cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", f"add {name}", cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo)


DELETE_COPY = "diff --git a/copy.py b/copy.py\ndeleted file mode 100644\n--- a/copy.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"


@pytest.fixture
def shared(tmp_path: Path) -> tuple[Path, str]:
    """A checkout at main, and the phase tip: one commit past main that adds `copy.py`, on a branch not checked out."""
    repo = tmp_path / "app"
    _git("init", "-q", "-b", "main", str(repo), cwd=tmp_path)
    _commit(repo, "app.py", "a\n")
    _git("checkout", "-q", "-b", "phase", cwd=repo)
    tip = _commit(repo, "copy.py", "x\n")
    _git("checkout", "-q", "main", cwd=repo)
    return repo, tip


def _build(runner: object) -> dict:
    return dict(runner.run(role="build", schema={}, prompt="revise", task="p1-rev-1"))  # type: ignore[attr-defined]


def _answers() -> dict:
    return {TREE: True, "build": {"patch": DELETE_COPY, "summary": "revised", "files_touched": ["copy.py"]}}


def test_a_build_at_the_shared_checkouts_head_cannot_revise_the_phases_own_file(shared):
    repo, _ = shared
    runner = ScriptedRunner(_answers(), repo)
    with pytest.raises(RunnerError, match="node 'build' patch_outside_scratch"):
        _build(runner)


def test_the_builder_runner_reads_the_tree_at_the_tasks_base_and_leaves_the_shared_runner_alone(shared, tmp_path):
    repo, tip = shared
    runner = ScriptedRunner(_answers(), repo)
    tree = tmp_path / "tree"
    _git("worktree", "add", "-q", "--detach", str(tree), tip, cwd=repo)
    built = _build(phase_lane.builder_runner(runner, tree))
    assert built["patch"].startswith("diff --git a/copy.py b/copy.py\ndeleted file mode 100644"), built["patch"]
    assert runner.repo_dir == repo and [c["role"] for c in runner.calls] == ["build"]  # the view records on the runner
    assert _git("worktree", "list", "--porcelain", cwd=repo).count("worktree ") == 2  # the scratch is gone again
    assert _git("status", "--porcelain", cwd=tree) == ""  # the build never touched the tree itself


def test_a_scripted_runner_without_the_tree_mark_has_no_tree_and_replays_the_patch_as_given(shared, tmp_path):
    repo, _ = shared
    runner = ScriptedRunner({"build": _answers()["build"]}, repo)
    assert not hasattr(runner, "repo_dir")
    assert _build(phase_lane.builder_runner(runner, tmp_path))["patch"] == DELETE_COPY
