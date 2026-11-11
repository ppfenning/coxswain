"""A `git worktree prune` in one lane never runs while another lane's `worktree add` is still in flight.

Two phases of one wave open their worktrees on two threads over one repository. Without a lock, one
lane's prune deleted the `.git/worktrees/<name>` entry the other lane's add had just made, and the add
failed with "gitfile does not point to a valid repository" (a phase blocked, or a task quarantined).
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from harness import worktree as wt


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_a_prune_waits_for_an_add_in_flight_on_another_thread(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "-c", "user.name=a", "-c", "user.email=a@b", "commit", "-q", "--allow-empty", "-m", "base")

    real = subprocess.run
    in_add, release = threading.Event(), threading.Event()
    order: list[str] = []

    def run(cmd, *args, **kwargs):
        if "add" in cmd:
            in_add.set()
            assert release.wait(10), "the test never released the add"
            order.append("add")
        elif "prune" in cmd:
            order.append("prune")
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(wt.subprocess, "run", run)
    results: dict[str, tuple[bool, str]] = {}
    adder = threading.Thread(
        target=lambda: results.update(add=wt.create_worktree(repo, tmp_path / "wt", branch="lane-a", base="main"))
    )
    pruner = threading.Thread(target=lambda: results.update(prune=wt.prune_registrations(repo)))
    adder.start()
    assert in_add.wait(10)
    pruner.start()
    pruner.join(0.3)
    assert pruner.is_alive(), "the prune ran while the add held the worktree registry"
    release.set()
    adder.join(10)
    pruner.join(10)

    assert order == ["add", "prune"]
    assert results["add"][0], results["add"][1]
    assert results["prune"][0], results["prune"][1]
    assert (tmp_path / "wt" / ".git").is_file()
