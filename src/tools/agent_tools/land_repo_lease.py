"""Repo-scoped land lease: one land per repository at a time, over store_cli's lease API.

Distinct from land_lease.py's task-scoped `land:<task>` lease. This lease is named
`land:<repo>` and is held for the duration of a land against that repository, not a task.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from agent_tools import store_cli


def lease_name(repo: str) -> str:
    return f"land:{repo}"


def refusal_message(repo: str, holder: str) -> str:
    return f"land: refusing, {holder} is landing in {repo}"


def takeover_message(holder: str) -> str:
    return f"land: taking over the repo lease from {holder} (pid gone)"


def holder_pid_gone(holder: str | None, this_host: str, alive: Callable[[int], bool]) -> bool:
    """True only for a `<label>@<host>:<pid>` holder on `this_host` whose pid is gone. A pid on another host cannot be probed."""
    label, _, tail = (holder or "").rpartition("@")
    host, _, pid = tail.partition(":")
    return bool(label) and host == this_host and pid.isdigit() and int(pid) > 0 and not alive(int(pid))


def acquire(runs_dir: Path, repo: str, holder: str, ttl: int) -> store_cli.LeaseResult:
    return store_cli.lease_acquire(runs_dir, lease_name(repo), holder, ttl)


def take_over(runs_dir: Path, repo: str, holder: str, ttl: int) -> store_cli.LeaseResult:
    """Acquire at a higher epoch. Not a compare-and-swap: a racing land that also steals is caught by its next renew."""
    return store_cli.lease_acquire(runs_dir, lease_name(repo), holder, ttl, steal=True)


def renew(runs_dir: Path, repo: str, holder: str, epoch: int, ttl: int) -> store_cli.LeaseResult:
    return store_cli.lease_renew(runs_dir, lease_name(repo), holder, epoch, ttl)


def release(runs_dir: Path, repo: str, holder: str, epoch: int) -> store_cli.LeaseResult:
    return store_cli.lease_release(runs_dir, lease_name(repo), holder, epoch)
