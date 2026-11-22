"""F10: a `git worktree add` that hangs under the registry lock times out, takes every process it started with it,
leaves no registration behind and lets the lock go."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from core.bounded_proc import TIMEOUT_RETURNCODE, completed_in_group
from harness.worktree import registry_lock
from runner.claude_scratch import drop_scratch, make_scratch
from runner.protocol import RunnerError


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def _repo_whose_checkout_hangs(path: Path, pidfile: Path) -> Path:
    """A real repository whose checkout runs a smudge filter that records its pid and sleeps.

    `worktree add` runs that checkout in a child `git reset --hard`, after it has registered the worktree and locked
    the entry "initializing" and while it holds the new index's lock: the state a hung add leaves behind.
    """
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    _git(path, "config", "filter.slow.clean", "cat")
    (path / ".gitattributes").write_text("*.txt filter=slow\n", encoding="utf-8")
    (path / "a.txt").write_text("a\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "a")
    _git(path, "config", "filter.slow.smudge", f"sh -c 'echo $$ > {pidfile}; exec sleep 60'")
    return path


def _gone(pid: int, within: float = 5.0) -> bool:
    """Whether `pid` has exited (or is only a zombie awaiting its reaper) within `within` seconds."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            if Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].startswith("Z"):
                return True
        except (FileNotFoundError, IndexError):
            return True
        time.sleep(0.05)
    return False


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


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="reads process state from /proc")
def test_a_hung_worktree_add_is_killed_whole_and_leaves_no_registration(tmp_path: Path) -> None:
    pidfile = tmp_path / "smudge.pid"
    repo = _repo_whose_checkout_hangs(tmp_path / "repo", pidfile)

    start = time.monotonic()
    with pytest.raises(RunnerError, match="timed out after 2 seconds"):
        make_scratch(repo, "build", timeout=2)
    assert time.monotonic() - start < 20

    assert pidfile.exists(), "the add never reached its checkout, so this proved nothing"
    assert _gone(int(pidfile.read_text(encoding="utf-8")))
    assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert not any((repo / ".git" / "worktrees").glob("*"))
    assert _lock_is_free(repo)


def test_a_hung_worktree_remove_returns_within_its_bound(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    real = shutil.which("git")
    assert real is not None
    (tmp_path / "bin").mkdir()
    stand_in = tmp_path / "bin" / "git"
    stand_in.write_text(f'#!/bin/sh\nfor a in "$@"; do [ "$a" = worktree ] && exec sleep 60; done\nexec {real} "$@"\n')
    stand_in.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")

    start = time.monotonic()
    drop_scratch(repo, tmp_path / "parent", tmp_path / "parent" / "tree", timeout=1)
    assert time.monotonic() - start < 15
    assert _lock_is_free(repo)


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="reads process state from /proc")
def test_a_timed_out_shell_call_takes_its_children_with_it(tmp_path: Path) -> None:
    """The scratch's diff is a `sh -c` pipeline: the shell is not the only process a timeout must end."""
    pidfile = tmp_path / "child.pid"
    proc = completed_in_group(f"sleep 60 & echo $! > {pidfile}; wait", timeout=1, shell=True, capture_output=True, text=True)
    assert (proc.returncode, proc.stderr) == (TIMEOUT_RETURNCODE, "sleep 60 timed out after 1 seconds")
    assert _gone(int(pidfile.read_text(encoding="utf-8")))
