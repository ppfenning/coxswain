"""The phase lane's own clone, against real git: when it is made, when the lane falls back to the shared checkout,
and which dead runs' clones a starting lane removes. The rest of the lane is `smoke/test_phase_lane.py`'s."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from harness import phase_lane, repo_cache
from harness.epic import Ctx


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _ctx(repo: Path, tmp_path: Path, run_id: str = "ship-p1-2") -> Ctx:
    return Ctx(
        repo=repo, cartridge={}, runner=None, specs={}, run_id=run_id, date="d",
        max_parallel=1, ledger_path=tmp_path / "ledger.jsonl", provider_profile="p",
        runs_dir=tmp_path / "runs", worktree_root=tmp_path / "worktrees", assume=None,
        fix_attempts=None, initiative_id="ship", default_ref="main",
    )


@pytest.fixture
def shared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The machine's shared checkout of a bare origin, named by a relative path; the cache is the test's own."""
    monkeypatch.setenv(repo_cache.CACHE_ENV, str(tmp_path / "cache"))
    origin, checkout = tmp_path / "origin.git", tmp_path / "src" / "app"
    _git("init", "-q", "--bare", "-b", "main", str(origin))
    _git("init", "-q", "-b", "main", str(checkout))
    _git("remote", "add", "origin", "../../origin.git", cwd=checkout)
    (checkout / "README.md").write_text("app\n", encoding="utf-8")
    _git("add", "README.md", cwd=checkout)
    _git("commit", "-qm", "init", cwd=checkout)
    _git("push", "-q", "origin", "main", cwd=checkout)
    return checkout


def test_the_lane_clones_origin_into_its_run_folder_and_keeps_the_shared_hooks(shared, tmp_path):
    said: list[str] = []
    ctx = _ctx(shared, tmp_path)
    own = phase_lane._own_clone(ctx, said.append)
    assert own.repo == repo_cache.lane_clone_dir(ctx.worktree_root, ctx.run_id) and own.pool is None
    assert _git("remote", "get-url", "origin", cwd=own.repo) == str(tmp_path / "origin.git")  # origin, not the mirror
    assert _git("config", "core.hooksPath", cwd=own.repo) == str(shared / ".git" / "hooks")
    assert any("mirror created" in line for line in said), said


def test_no_origin_leaves_the_lane_in_the_shared_checkout(shared, tmp_path):
    _git("remote", "remove", "origin", cwd=shared)
    said: list[str] = []
    ctx = _ctx(shared, tmp_path)
    assert phase_lane._own_clone(ctx, said.append) is ctx
    assert "no origin to clone from" in said[0]
    assert not (ctx.worktree_root / ctx.run_id).exists()


def test_a_failed_clone_leaves_the_lane_in_the_shared_checkout_and_nothing_behind(shared, tmp_path):
    _git("remote", "set-url", "origin", str(tmp_path / "gone.git"), cwd=shared)
    said: list[str] = []
    ctx = _ctx(shared, tmp_path)
    assert phase_lane._own_clone(ctx, said.append) is ctx
    assert any("its own clone failed" in line for line in said), said
    assert not repo_cache.lane_clone_dir(ctx.worktree_root, ctx.run_id).exists()


def test_a_starting_lane_removes_dead_runs_clones_of_any_phase_and_never_a_live_one(shared, tmp_path):
    ctx = _ctx(shared, tmp_path, run_id="ship-p1-5")
    root = ctx.worktree_root
    # A live lane of another phase, with no pidfile at all: it holds its lock, as `run_lane` does from its start.
    live = phase_lane._hold_run_lock(_ctx(shared, tmp_path, run_id="live-p2-1"))
    try:
        for run in ("other-p9-1", "ship-p1-3", "live-p2-1", "ship-p1-5"):
            (root / run / repo_cache.LANE_CLONE).mkdir(parents=True, exist_ok=True)
        for run in ("ship-p1-3",):  # a dead lane that took its lock: the kernel dropped it when the lane died
            repo_cache.lane_lock_file(root, run).write_text("", encoding="utf-8")
        (root / "old-model-1" / "p1").mkdir(parents=True)  # no clone: not ours to judge
        assert phase_lane._sweep_dead_clones(ctx) == ["other-p9-1", "ship-p1-3"]
        assert sorted(p.name for p in root.iterdir()) == ["live-p2-1", "old-model-1", "ship-p1-5"]
        assert (root / "live-p2-1" / repo_cache.LANE_CLONE).is_dir()
    finally:
        os.close(live)
    assert phase_lane._sweep_dead_clones(ctx) == ["live-p2-1"]  # its lane gone, its lock free: swept
