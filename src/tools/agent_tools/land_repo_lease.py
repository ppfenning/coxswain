"""Repo-scoped land lease: one land per repository at a time, over store_cli's lease API.

Distinct from land_lease.py's task-scoped `land:<task>` lease. This lease is named
`land:<repo>` and is held for the duration of a land against that repository, not a task.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from agent_tools import store_cli


def lease_name(repo: str) -> str:
    return f"land:{repo}"


def refusal_message(repo: str, holder: str) -> str:
    return f"land: refusing, {holder} is landing in {repo}"


def acquire(runs_dir: Path, repo: str, holder: str, ttl: int) -> store_cli.LeaseResult:
    return store_cli.lease_acquire(runs_dir, lease_name(repo), holder, ttl)


def holder_host_pid(holder: str | None) -> tuple[str, int] | None:
    """`(host, pid)` of a `<label>@<host>:<pid>` holder, None for anything else, such as `another land`."""
    head, colon, pid = (holder or "").rpartition(":")
    _label, at, host = head.rpartition("@")
    return (host, int(pid)) if colon and at and host and pid.isdigit() and int(pid) > 0 else None


def pid_gone(holder: str | None, this_host: str, alive: Callable[[int], bool]) -> bool:
    """True only for a holder on `this_host` whose pid `alive` denies; another host's pid says nothing here."""
    parsed = holder_host_pid(holder)
    return parsed is not None and parsed[0] == this_host and not alive(parsed[1])


def pid_alive(pid: int) -> bool:
    """Edge. Signal 0 probes without sending; a pid owned by another user exists, so it counts as alive."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_or_take_over(runs_dir: Path, repo: str, holder: str, ttl: int, this_host: str,
                         alive: Callable[[int], bool] = pid_alive) -> store_cli.LeaseResult:
    """`acquire`, and when the lease is held by a dead pid on `this_host`, one `--steal` over it.

    The store's steal replaces whatever it reads at that moment, not the holder this call was refused by. A takeover
    is granted at the refused epoch + 1; any other epoch means another land took it first, and this call just took
    that live land's lease. It is then released, so the other land fails its next renew and stops, and the refusal is
    returned with no holder named."""
    got = acquire(runs_dir, repo, holder, ttl)
    if not (isinstance(got, store_cli.LeaseRefused) and pid_gone(got.holder, this_host, alive)):
        return got
    taken = store_cli.lease_acquire(runs_dir, lease_name(repo), holder, ttl, steal=True)
    if not isinstance(taken, store_cli.LeaseGranted):
        return taken
    if got.epoch is None or taken.epoch != got.epoch + 1:
        release(runs_dir, repo, holder, taken.epoch)
        return store_cli.LeaseRefused(got.epoch, None)
    print(f"land: taking over the repo lease from {got.holder} (pid gone)")
    return taken


def renew(runs_dir: Path, repo: str, holder: str, epoch: int, ttl: int) -> store_cli.LeaseResult:
    return store_cli.lease_renew(runs_dir, lease_name(repo), holder, epoch, ttl)


def release(runs_dir: Path, repo: str, holder: str, epoch: int) -> store_cli.LeaseResult:
    return store_cli.lease_release(runs_dir, lease_name(repo), holder, epoch)
