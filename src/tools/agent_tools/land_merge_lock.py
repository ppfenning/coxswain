"""Short per-repo merge lock with a bounded wait.

The lease is named `merge:<repo>`, never `land:<repo>` or a `land:<repo>#<k>` slot, so holding it
cannot collide with a land slot. The ttl is the caller's; it should be one merge, so a crashed
holder frees the lock soon. There is no renewal here.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from agent_tools import store_cli

POLL_SECONDS = 0.5


@dataclass(frozen=True)
class Held:
    epoch: int
    holder: str


@dataclass(frozen=True)
class NotHeld:
    reason: str
    holder: str | None = None


def merge_lock_name(repo: str) -> str:
    return f"merge:{repo}"


def _wait_for_grant(acquire: Callable[[], store_cli.LeaseResult], sleep: Callable[[float], None],
                    clock: Callable[[], float], timeout: float, poll: float) -> store_cli.LeaseGranted | NotHeld:
    start = clock()
    while True:
        got = acquire()
        if isinstance(got, store_cli.LeaseGranted):
            return got
        if isinstance(got, store_cli.LeaseError):
            return NotHeld(f"store outage: {got.detail}")
        if isinstance(got, store_cli.NotAvailable):
            return NotHeld(f"store outage: {got.reason}")
        if not isinstance(got, store_cli.LeaseRefused):
            return NotHeld(f"unexpected lease answer: {type(got).__name__}")
        if clock() - start >= timeout:
            return NotHeld("timeout", got.holder)
        sleep(poll)


@contextmanager
def hold(runs_dir: Path, repo: str, holder: str, ttl: int, timeout: float, *,
         poll: float = POLL_SECONDS,
         sleep: Callable[[float], None] = time.sleep,
         clock: Callable[[], float] = time.monotonic,
         acquire: Callable[..., store_cli.LeaseResult] = store_cli.lease_acquire,
         release: Callable[..., store_cli.LeaseResult] = store_cli.lease_release) -> Iterator[Held | NotHeld]:
    """Yield Held once the lock is granted, or NotHeld after `timeout` seconds or on a store outage.

    Releases on exit, including on an exception, only when it was granted. A failed release is left
    to the ttl.
    """
    name = merge_lock_name(repo)
    got = _wait_for_grant(lambda: acquire(runs_dir, name, holder, ttl), sleep, clock, timeout, poll)
    if isinstance(got, NotHeld):
        yield got
        return
    try:
        yield Held(got.epoch, got.holder)
    finally:
        release(runs_dir, name, holder, got.epoch)
