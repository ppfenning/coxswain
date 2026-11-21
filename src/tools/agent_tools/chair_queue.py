"""One kind of the chair's work as its own process: `cox chair worker --kind K` (C3 of the event-driven chair).

The loop holds the kind's store lease (`chair:<kind>`, `chair_kinds.lease_name`), waits for a `cox_store`
notification on a table its kind reads or for its poll interval, then gathers the chair's facts, plans that kind with
`chair_plan.plan_kind` and performs the actions under its lease's epoch. It is the tick's own planner, run on its own,
so a pass decides exactly what the tick would have decided for that kind.

The rules, each a pure function below:

- **One per kind.** A worker acts only while it holds the kind lease. A second copy is refused the lease and stands by,
  retrying each beat, so it takes over within one lease time-to-live of the first one stopping. The lease is renewed
  every `beat` seconds and once more right before a pass performs, so a worker that stalled past its lease's expiry
  performs nothing.
- **The setting decides.** A worker plans only a kind the profile's `chair.queues` lists, and lets the lease go when
  the kind is removed, so the tick (`chair_kinds.tick_skips`) takes the kind back.
- **A wake is a hint.** A notification for one of the kind's tables makes a pass due once `min_gap` has passed since
  the last one; with none, a pass is due every `poll` seconds. A notification lost to a dropped connection costs
  latency, never a decision, since each pass re-reads the facts.

A dry run (`--dry-run`) takes no lease and ignores the setting: it plans as the kind's worker would and performs
nothing, so its plan can be compared with the tick's (§5.3 of the plan).
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from agent_tools import chair_kinds
from agent_tools.chair_beat import beat_in_thread
from agent_tools.chair_exec import Result
from agent_tools.chair_plan import plan_kind
from agent_tools.chair_types import Action, Facts, stamp

__all__ = [
    "BEAT_SECONDS",
    "LEASE_TTL_SECONDS",
    "MIN_GAP_SECONDS",
    "Loop",
    "Ports",
    "Timing",
    "heard",
    "lease_due",
    "pass_due",
    "pass_line",
    "pass_reason",
    "planned_pass",
    "run",
    "wait_seconds",
]

# The leases amendment (2026-11-20): heartbeat 10 s, lease 60 s.
BEAT_SECONDS = 10.0
LEASE_TTL_SECONDS = 60
# A burst of writes (or the worker's own) makes one pass, not one per commit: a host beat alone notifies every 30 s.
MIN_GAP_SECONDS = 10.0


@dataclass(frozen=True)
class Timing:
    poll: float  # the fallback: a pass at least this often with no notification
    min_gap: float = MIN_GAP_SECONDS  # a notified pass waits this long after the last one
    beat: float = BEAT_SECONDS  # the lease is renewed this often


@dataclass(frozen=True)
class Loop:
    """What the loop remembers between waits, on the monotonic clock."""

    last_pass: float | None = None  # None until the first pass, which is due at once
    woke: frozenset[str] = field(default_factory=frozenset[str])  # the kind's tables notified since the last pass
    last_beat: float | None = None  # the last acquire or renew of the kind lease


def heard(loop: Loop, kind: str, tables: frozenset[str]) -> Loop:
    """Pure. The loop after a wait that heard `tables`: only those the kind reads are kept."""
    mine = tables & chair_kinds.WAKE_TABLES.get(kind, frozenset())
    return replace(loop, woke=loop.woke | mine) if mine else loop


def pass_due(loop: Loop, now: float, timing: Timing) -> bool:
    """Pure. The first pass at once; then on a kept notification once `min_gap` has passed, else every `poll`."""
    if loop.last_pass is None:
        return True
    since = now - loop.last_pass
    return since >= timing.poll or (bool(loop.woke) and since >= timing.min_gap)


def pass_reason(loop: Loop) -> str:
    """Pure. Why the pass about to run is due, for its status line."""
    if loop.last_pass is None:
        return "start"
    return f"notified: {', '.join(sorted(loop.woke))}" if loop.woke else "poll"


def lease_due(loop: Loop, now: float, timing: Timing) -> bool:
    """Pure. Whether the kind lease should be renewed now."""
    return loop.last_beat is None or now - loop.last_beat >= timing.beat


def wait_seconds(loop: Loop, now: float, timing: Timing) -> float:
    """Pure. Seconds to wait for a notification before the loop must look again: the next poll, the next beat, or, with
    a notification kept, the end of the gap. Never negative."""
    deadlines = [timing.beat - (now - loop.last_beat) if loop.last_beat is not None else 0.0]
    if loop.last_pass is not None:
        since = now - loop.last_pass
        deadlines.append(timing.poll - since)
        if loop.woke:
            deadlines.append(timing.min_gap - since)
    return max(0.0, min(deadlines))


def planned_pass(kind: str, facts: Facts, now: datetime, epoch: int, dry_run: bool) -> list[Action] | None:
    """Pure. The actions of one pass, each stamped with the kind lease's epoch; None when the facts say the setting
    does not list the kind (a live pass never plans a kind the tick may still be planning). A dry run plans anyway."""
    if not dry_run and not chair_kinds.worker_acts(kind, facts):
        return None
    return [stamp(a, epoch) for a in plan_kind(kind, facts, now)]


def pass_line(
    kind: str, reason: str, actions: Sequence[Action] | None, results: Sequence[Result], dry_run: bool, at: datetime | None = None
) -> str:
    """Pure. One status line for a pass; `at`, the pass's start, is written in UTC to the millisecond."""
    when = f" {at.astimezone(UTC):%Y-%m-%dT%H:%M:%S}.{at.microsecond // 1000:03d}Z" if at is not None else ""
    head = f"chair worker {kind}{' (dry run)' if dry_run else ''}{when} | {reason}"
    if actions is None:
        return f"{head} | not queued: chair.queues does not list {kind}; the tick plans it"
    if not actions:
        return f"{head} | nothing due"
    counts: dict[str, int] = {}
    for r in results:
        key = f"{r['action'].get('kind')} {r['status']}"
        counts[key] = counts.get(key, 0) + 1
    return f"{head} | planned {len(actions)} | " + ", ".join(f"{n}x {k}" for k, n in sorted(counts.items()))


@dataclass(frozen=True)
class Ports:
    """The worker's edges. `acquire` returns the lease's epoch or None when another holder has it; `renew` whether the
    epoch still holds; `wait` the tables notified within the timeout (a plain sleep returns none)."""

    queued: Callable[[], frozenset[str]]
    acquire: Callable[[], int | None]
    renew: Callable[[int], bool]
    release: Callable[[int], None]
    gather: Callable[[datetime], Facts]
    perform: Callable[[list[Action], Callable[[], int], bool], list[Result]]
    wait: Callable[[float], frozenset[str]]
    clock: Callable[[], float]
    now: Callable[[], datetime]
    echo: Callable[[str], None]


@dataclass
class _Held:
    """Edge state: the kind lease's epoch while this process holds it; read by the fence on every action."""

    epoch: int | None = None


def _performed(kind: str, ports: Ports, held: _Held, reason: str, dry_run: bool, timing_beat: float = BEAT_SECONDS) -> str:
    """Edge. One pass: gather, plan, renew (the fence), perform."""
    now = ports.now()
    facts = ports.gather(now)
    epoch = -1 if dry_run else (held.epoch if held.epoch is not None else -1)
    actions = planned_pass(kind, facts, now, epoch, dry_run)
    if not actions:
        return pass_line(kind, reason, actions, [], dry_run, now)
    if not dry_run and (held.epoch is None or not ports.renew(held.epoch)):
        held.epoch = None
        return pass_line(kind, reason, actions, [], dry_run, now) + " | lease lost before perform: nothing performed"
    current = (lambda: -1) if dry_run else (lambda: held.epoch if held.epoch is not None else -2)
    stop = threading.Event()
    if not dry_run:  # a revert can outlast the lease: renew it while the actions run, and fence the rest once it is lost
        beat_in_thread(lambda: _renew_or_drop(ports, held, stop), timing_beat, stop, lambda _exc: _drop(held, stop))
    try:
        results = ports.perform(actions, current, dry_run)
    finally:
        stop.set()
    return pass_line(kind, reason, actions, results, dry_run, now)


def _renew_or_drop(ports: Ports, held: _Held, stop: threading.Event) -> None:
    """Edge. The beat during a perform: a refused renew drops the epoch, so every later action is fenced."""
    epoch = held.epoch
    if epoch is not None and not ports.renew(epoch):
        _drop(held, stop)


def _drop(held: _Held, stop: threading.Event) -> None:
    """Edge. A renew refused, or one that raised: the lease may pass to another worker, so the rest is fenced."""
    held.epoch = None
    stop.set()


def _hold(kind: str, ports: Ports, held: _Held, loop: Loop, now: float, timing: Timing) -> tuple[Loop, str]:
    """Edge. Takes or renews the kind lease when due; returns the loop and a line for a change of standing, or ""."""
    if not lease_due(loop, now, timing):
        return loop, ""
    try:
        if held.epoch is not None and ports.renew(held.epoch):
            return replace(loop, last_beat=now), ""
        had = held.epoch is not None
        held.epoch = ports.acquire()
    except Exception as exc:  # a store that does not answer holds nothing: stand by and try again next beat
        held.epoch = None
        return replace(loop, last_beat=now), f"chair worker {kind} | lease check failed: {type(exc).__name__}: {exc}"
    if held.epoch is not None:
        return replace(loop, last_beat=now), f"chair worker {kind} | holds {chair_kinds.lease_name(kind)} at epoch {held.epoch}"
    return replace(loop, last_beat=now), (
        f"chair worker {kind} | lost {chair_kinds.lease_name(kind)}; standing by" if had else ""
    )


def run(kind: str, ports: Ports, timing: Timing, once: bool, dry_run: bool) -> int:
    """Edge. The worker loop until interrupted (KeyboardInterrupt, which SIGTERM raises), or one pass with `once`.

    Returns 0, or 1 when `once` could not act: the setting does not list the kind or another holder has its lease.
    The lease is released on every way out."""
    if kind not in chair_kinds.QUEUE_KINDS:
        ports.echo(f"chair worker: no queue worker for kind {kind!r}; one of: {', '.join(chair_kinds.QUEUE_KINDS)}")
        return 2
    held = _Held()
    loop = Loop()
    standing = ""  # the last standing line printed, so a standby is said once, not every beat
    try:
        while True:
            now = ports.clock()
            if not dry_run and kind not in ports.queued():
                if held.epoch is not None:
                    with contextlib.suppress(Exception):
                        ports.release(held.epoch)
                    held.epoch, loop = None, replace(loop, last_beat=None)
                line = f"chair worker {kind} | idle: chair.queues does not list {kind}; the tick plans it"
                if line != standing:
                    ports.echo(line)
                    standing = line
                if once:
                    return 1
                loop = heard(loop, kind, ports.wait(timing.beat))
                continue
            if not dry_run:
                loop, line = _hold(kind, ports, held, loop, now, timing)
                if line:
                    ports.echo(line)
                if held.epoch is None:
                    line = f"chair worker {kind} | standby: another worker holds {chair_kinds.lease_name(kind)}"
                    if line != standing:
                        ports.echo(line)
                        standing = line
                    if once:
                        return 1
                    loop = heard(loop, kind, ports.wait(max(0.0, wait_seconds(loop, ports.clock(), timing))))
                    continue
                standing = ""
            else:
                loop = replace(loop, last_beat=now)  # a dry run holds no lease: nothing to renew
            if pass_due(loop, now, timing):
                reason = pass_reason(loop)
                try:
                    ports.echo(_performed(kind, ports, held, reason, dry_run, timing.beat))
                except Exception as exc:  # one bad pass must not stop the worker; the next is due on the poll
                    ports.echo(f"chair worker {kind} | {reason} | error: {type(exc).__name__}: {exc}")
                loop = replace(loop, last_pass=ports.clock(), woke=frozenset())
                if once:
                    return 0
            loop = heard(loop, kind, ports.wait(wait_seconds(loop, ports.clock(), timing)))
    except KeyboardInterrupt:
        return 0
    finally:
        if held.epoch is not None:
            with contextlib.suppress(Exception):
                ports.release(held.epoch)


def store_waiter(url: str, sleep: Callable[[float], None]) -> tuple[Callable[[float], frozenset[str]], Callable[[], None]]:
    """Edge. A `wait` that listens for `cox_store` notifications on the store at `url` (`harness.store_notify`, an
    autocommit connection that drains everything already arrived on each wake), and the function that closes it.

    sqlite has no NOTIFY, so there `wait` is a plain sleep. A listener that fails (a dropped connection) is closed and
    the wait sleeps out its timeout; the next wait listens again. Polling covers what was missed meanwhile."""
    from harness import store_notify  # the harness loads only in a worker process

    listener: list[store_notify.Listener | None] = [None]  # edge state: the open listener, if any
    postgres = [True]  # edge state: False once `listen` has said the store is sqlite

    def close() -> None:
        if listener[0] is not None:
            with contextlib.suppress(Exception):
                listener[0].close()
            listener[0] = None

    def wait(timeout: float) -> frozenset[str]:
        if postgres[0] and listener[0] is None:
            try:
                listener[0] = store_notify.listen(url)
                postgres[0] = listener[0] is not None
            except Exception:
                listener[0] = None
        if listener[0] is None:
            sleep(timeout)
            return frozenset()
        try:
            return listener[0].wait(timeout)
        except Exception:
            close()
            sleep(timeout)
            return frozenset()

    return wait, close
