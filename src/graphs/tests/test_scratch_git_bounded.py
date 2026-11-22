"""F10: a hung `git worktree` under the registry lock times out and lets the lock go."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from harness.worktree import registry_lock
from runner.claude_scratch import drop_scratch, make_scratch
from runner.protocol import RunnerError


def _repo(path: Path) -> Path:
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(path)]
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "a.txt").write_text("a\n", encoding="utf-8")
    subprocess.run([*git, "add", "a.txt"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "a"], check=True)
    return path


def _hung_git(bin_dir: Path) -> None:
    """A `git` on PATH that hangs on any `worktree` call and is the real git otherwise."""
    real = shutil.which("git")
    assert real is not None
    bin_dir.mkdir()
    script = bin_dir / "git"
    script.write_text(
        f'#!/bin/sh\nfor a in "$@"; do [ "$a" = worktree ] && exec sleep 60; done\nexec {real} "$@"\n', encoding="utf-8"
    )
    script.chmod(0o755)


def _lock_is_free(repo: Path) -> bool:
    """Whether another thread can take the registry lock (the thread lock and the flock) within five seconds."""
    taken = threading.Event()

    def take() -> None:
        with registry_lock(repo):
            taken.set()

    thread = threading.Thread(target=take, daemon=True)
    thread.start()
    thread.join(5)
    return taken.is_set()


def test_a_hung_worktree_add_times_out_and_releases_the_registry_lock(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path / "repo")
    _hung_git(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ["PATH"]}")

    start = time.monotonic()
    with pytest.raises(RunnerError, match="timed out after 1 seconds"):
        make_scratch(repo, "build", timeout=1)
    assert time.monotonic() - start < 15
    assert _lock_is_free(repo)

    start = time.monotonic()
    drop_scratch(repo, tmp_path / "parent", tmp_path / "parent" / "tree", timeout=1)
    assert time.monotonic() - start < 15
    assert _lock_is_free(repo)
