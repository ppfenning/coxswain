"""Warm pool edge: lease, release and trim pooled worktrees on one host.

Joins the pure rules in `warm_pool` to the git primitives in `worktree`. State is one
JSON index per repository at `pool_root/<repo dir name>/index.json`. Several lane runs on
one host share it, so every read-modify-write holds an exclusive `flock` on a sidecar
file and replaces the index atomically. The lock is never held across a sync.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import subprocess
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal

from harness.warm_pool import PoolEntry, choose_evictions, lock_digest, sync_decision
from harness.worktree import (
    create_worktree,
    link_venv,
    prune_registrations,
    read_lock,
    remove_worktree,
    reset_worktree,
    sync_env,
)

INDEX_NAME = "index.json"
LOCK_NAME = "index.lock"
# A leased entry younger than this is a live run; older with its directory still on disk is a crash.
DEFAULT_STALE_AGE = 6 * 3600.0


class PoolError(RuntimeError):
    """A lease could not be set up; the message is the failing step's own output."""


@dataclass(frozen=True)
class Lease:
    path: Path
    slot: str
    mode: Literal["warm", "cold"]
    setup_seconds: float
    leased_at: float
    repo: Path
    pool_root: Path
    clock: Callable[[], float]


def _pool_dir(repo: Path, pool_root: Path) -> Path:
    return Path(pool_root) / Path(repo).resolve().name


def next_slot(entries: Sequence[PoolEntry]) -> str:
    """First unused `slot-N`, counting from 1."""
    taken = {e.slot for e in entries}
    return next(f"slot-{n}" for n in range(1, len(taken) + 2) if f"slot-{n}" not in taken)


def stamp(now: float, prior: float) -> float:
    """`now`, bumped past `prior`: a slot's stamps strictly rise even under a frozen clock.

    A lease owns its slot while the entry is leased at exactly its stamp, so equal stamps would blur owners.
    """
    return max(now, math.nextafter(prior, math.inf))


def pick_reusable(
    entries: Sequence[PoolEntry], now: float, stale_age: float, holder: str | None = None
) -> PoolEntry | None:
    """Idle entry first: the slot already holding the wanted branch, else the latest used.

    With none idle, the oldest leased entry whose lease is older than `stale_age`.
    """
    idle = [e for e in entries if not e.leased]
    stale = [e for e in entries if e.leased and now - e.last_used > stale_age]
    if idle:
        return max(idle, key=lambda e: (e.slot == holder, e.last_used, e.slot))
    if stale:
        return min(stale, key=lambda e: (e.last_used, e.slot))
    return None


def _git_out(path: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def _current_branch(path: Path) -> str:
    return _git_out(path, "branch", "--show-current")


def _is_worktree(path: Path) -> bool:
    """True when `path` is itself a git working tree, not a plain or half-made directory."""
    top = _git_out(path, "rev-parse", "--show-toplevel")
    return bool(top) and Path(top).resolve() == path.resolve()


def _drop_branch(repo: Path, branch: str, *, force: bool) -> None:
    """Delete a local branch; the safe form (`-d`) refuses a branch with unmerged work."""
    if branch:
        subprocess.run(["git", "-C", str(repo), "branch", "-D" if force else "-d", branch], capture_output=True)


def _read_index(pool_dir: Path) -> list[PoolEntry]:
    index = pool_dir / INDEX_NAME
    raw = json.loads(index.read_text(encoding="utf-8")) if index.exists() else {"entries": []}
    return [PoolEntry(**e) for e in raw["entries"]]


def _write_index(pool_dir: Path, entries: Sequence[PoolEntry]) -> None:
    tmp = pool_dir / f"{INDEX_NAME}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps({"entries": [asdict(e) for e in entries]}, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, pool_dir / INDEX_NAME)


@contextmanager
def _locked(pool_dir: Path) -> Iterator[None]:
    pool_dir.mkdir(parents=True, exist_ok=True)
    with open(pool_dir / LOCK_NAME, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _update[T](pool_dir: Path, change: Callable[[list[PoolEntry]], tuple[list[PoolEntry], T]]) -> T:
    """Read the index, apply `change`, write the result, all under the exclusive lock."""
    with _locked(pool_dir):
        entries, result = change(_read_index(pool_dir))
        _write_index(pool_dir, entries)
        return result


def _drop_vanished(repo: Path, entries: Sequence[PoolEntry]) -> list[PoolEntry]:
    live = [e for e in entries if Path(e.path).is_dir()]
    if len(live) != len(entries):
        # A registration whose directory is gone would block `worktree add` at the same path.
        prune_registrations(repo)
    return live


def _replace_slot(entries: Sequence[PoolEntry], slot: str, **changes: object) -> list[PoolEntry]:
    return [replace(e, **changes) if e.slot == slot else e for e in entries]


def acquire(
    repo: Path,
    pool_root: Path,
    *,
    branch: str,
    base: str,
    sync_argv: Sequence[str],
    lock_names: Sequence[str],
    keep: int,
    clock: Callable[[], float],
    stale_age: float = DEFAULT_STALE_AGE,
) -> Lease:
    """Lease a worktree on `branch` at `base`; `keep` is the most entries the pool holds.

    Warm when an idle (or stale-leased) entry exists, cold when a new one fits. A reused
    slot that is not a working tree is discarded and the next candidate, or cold, is tried.
    """
    pool_dir = _pool_dir(repo, pool_root)
    started = clock()

    def reserve(entries: list[PoolEntry]) -> tuple[list[PoolEntry], tuple[PoolEntry, str, PoolEntry] | None]:
        live = _drop_vanished(repo, entries)
        # Git allows a branch in one worktree only, so reuse the slot that already holds it.
        holder = next((e.slot for e in live if not e.leased and _current_branch(Path(e.path)) == branch), None)
        picked = pick_reusable(live, started, stale_age, holder)
        if picked is not None:
            # A crashed run's environment is unknown, so a reclaimed lease always resyncs.
            digest = None if picked.leased else picked.synced_digest
            taken = replace(picked, leased=True, last_used=stamp(started, picked.last_used), synced_digest=digest)
            return [taken if e.slot == picked.slot else e for e in live], (taken, "warm", picked)
        if len(live) >= keep:
            return live, None
        slot = next_slot(live)
        path = pool_dir / slot
        # Creating the directory here keeps a concurrent acquire from reading the reservation as vanished.
        path.mkdir(parents=True, exist_ok=True)
        fresh = PoolEntry(path=str(path), slot=slot, synced_digest=None, last_used=started, leased=True)
        return [*live, fresh], (fresh, "cold", fresh)

    while True:
        reserved = _update(pool_dir, reserve)
        if reserved is None:
            raise PoolError(f"pool for {repo.name} is full: {keep} entries, none idle or stale")
        entry, mode, before = reserved
        ok, detail, broken = _set_up(
            repo, entry, mode, branch=branch, base=base, sync_argv=sync_argv, lock_names=lock_names
        )
        if ok:
            return Lease(
                path=Path(entry.path),
                slot=entry.slot,
                mode=mode,  # type: ignore[arg-type]
                setup_seconds=clock() - started,
                leased_at=entry.last_used,
                repo=repo,
                pool_root=Path(pool_root),
                clock=clock,
            )
        _abandon(repo, pool_dir, entry, before, discard=broken or mode == "cold")
        if not broken:
            raise PoolError(f"{mode} setup of {entry.slot} failed: {detail}")


def _set_up(
    repo: Path,
    entry: PoolEntry,
    mode: str,
    *,
    branch: str,
    base: str,
    sync_argv: Sequence[str],
    lock_names: Sequence[str],
) -> tuple[bool, str, bool]:
    """Edge: slow setup outside the index lock. Returns (ok, message, broken).

    `broken` is True only when a reused slot is not a working tree, so it must be discarded.
    """
    path = Path(entry.path)
    if mode == "cold":
        made, detail = create_worktree(repo, path, branch=branch, base=base)
        if not made:
            return False, detail, False
        link_venv(repo, path)
        synced, detail = sync_env(path, sync_argv)
        if not synced:
            # `worktree add -b` made the branch, so a retry of this lane would fail on it.
            remove_worktree(repo, path)
            _drop_branch(repo, branch, force=True)
        return synced, detail, False
    reset, detail = reset_worktree(path, branch=branch, base=base)
    if not reset:
        return False, detail, not _is_worktree(path)
    current = lock_digest(read_lock(path, lock_names))
    if sync_decision(entry.synced_digest, current) == "warm":
        return True, "", False
    synced, detail = sync_env(path, sync_argv)
    return synced, detail, False


def _abandon(repo: Path, pool_dir: Path, entry: PoolEntry, before: PoolEntry, *, discard: bool) -> None:
    """Undo a failed setup, only while the entry is still this lease's.

    Discard removes the slot (a failed cold create, or a reused slot that is no longer a working
    tree). Otherwise the slot goes back to idle and unsynced with its old `last_used`, so a
    failure cannot make it the newest entry and the first pick of every later acquire.
    """

    def ours(e: PoolEntry) -> bool:
        return e.slot == entry.slot and e.leased and e.last_used == entry.last_used

    if discard:
        remove_worktree(repo, Path(entry.path))
        _update(pool_dir, lambda entries: ([e for e in entries if not ours(e)], None))
    else:
        _update(
            pool_dir,
            lambda entries: (
                [replace(before, leased=False, synced_digest=None) if ours(e) else e for e in entries],
                None,
            ),
        )


def release(lease: Lease, synced_digest: str | None) -> bool:
    """Mark the leased entry idle, remembering the lock digest its environment was synced to.

    Returns False and changes nothing when the lease no longer owns its slot, because a
    stale-age reclaim gave it to another run: the entry must still be leased at `leased_at`.
    """
    now = lease.clock()

    def mark(entries: list[PoolEntry]) -> tuple[list[PoolEntry], bool]:
        owned = any(e.slot == lease.slot and e.leased and e.last_used == lease.leased_at for e in entries)
        idle = _replace_slot(entries, lease.slot, leased=False, synced_digest=synced_digest, last_used=now)
        return (idle if owned else entries), owned

    return _update(_pool_dir(lease.repo, lease.pool_root), mark)


def trim(repo: Path, pool_root: Path, keep: int, clock: Callable[[], float]) -> list[str]:
    """Remove idle entries beyond `keep`, oldest first; returns the slots removed.

    Three steps: claim the victims as leased and stamped with `clock()` under the lock, so a
    concurrent acquire cannot reclaim them as stale; remove them with the lock released; then
    settle the index. A failed removal goes back to idle for the next trim.
    The victim's branch is deleted with `-d`, so a branch holding unmerged work is kept.
    """
    pool_dir = _pool_dir(repo, pool_root)
    now = clock()

    def claim(entries: list[PoolEntry]) -> tuple[list[PoolEntry], list[PoolEntry]]:
        live = _drop_vanished(repo, entries)
        victims = set(choose_evictions(live, keep))
        claimed = [replace(e, leased=True, last_used=stamp(now, e.last_used)) if e.slot in victims else e for e in live]
        return claimed, [e for e in claimed if e.slot in victims]

    def evict(entry: PoolEntry) -> bool:
        branch = _current_branch(Path(entry.path))
        removed, _ = remove_worktree(repo, Path(entry.path))
        if removed:
            _drop_branch(repo, branch, force=False)
        return removed

    claimed = _update(pool_dir, claim)
    outcome = {e.slot: evict(e) for e in claimed}

    def settle(entries: list[PoolEntry]) -> tuple[list[PoolEntry], None]:
        kept = [replace(e, leased=False) if e.slot in outcome else e for e in entries if not outcome.get(e.slot)]
        return kept, None

    _update(pool_dir, settle)
    return [slot for slot, removed in outcome.items() if removed]
