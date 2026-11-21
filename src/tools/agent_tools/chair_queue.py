"""One kind of the chair's work as its own process: `cox chair worker --kind K` (C3 of the event-driven chair).

The loop holds the kind's store lease (`chair:<kind>`, `chair_kinds.lease_name`), waits for a `cox_store`
notification on a table its kind reads or for its poll interval, then gathers the chair's facts, plans that kind with
`chair_plan.plan_kind` and performs the actions under its lease's epoch. It is the tick's own planner, run on its own,
so a pass decides exactly what the tick would have decided for that kind.

Every decision of the loop is `decide`, a pure function of the loop's state and the clock; the edge (`run`) only does
what it says and folds the outcome back in (`beaten`, `passed`, `idled`, `lost`):

- **One per kind.** A worker acts only while it holds the kind lease, on the store's clock. A second copy is refused
  and stands by, trying again once per beat (never sooner), so it takes over within one lease time to live of the first
  one stopping. A lease check that fails, or does not answer within `call_timeout`, counts as not held.
- **Settle.** A worker that has just taken the lease waits `settle` seconds before its first pass, past the window in
  which a tick that read the leases before the take could still be performing that kind.
- **The setting decides.** A worker plans only a kind the profile's `chair.queues` lists, and lets the lease go when
  the kind is removed, so the tick (`chair_kinds.tick_skips`) takes the kind back.
- **A wake is a hint.** A notification for one of the kind's tables makes a pass due once `min_gap` has passed since
  the last one; with none, a pass is due every `poll` seconds. The listener is closed for the length of a pass (a watch
  revert can wait on checks for half an hour) and opened again for the next wait; a write during a pass is picked up
  by the next notification or the poll, since each pass re-reads the facts.
- **Fenced.** The lease is beaten on its own thread for the whole pass, the gather as well as the actions. Before each
  action the store is asked whether the epoch still holds; once it does not, that action and every later one are
  fenced, and each action's row is written only inside a transaction that holds the lease row (`record_fenced`).

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
from agent_tools.chair_exec_land import make_result
from agent_tools.chair_plan import plan_kind
from agent_tools.chair_types import Action, Facts, stamp

__all__ = [
    "BEAT_SECONDS",
    "CALL_TIMEOUT_SECONDS",
    "LEASE_TTL_SECONDS",
    "MIN_GAP_SECONDS",
    "SETTLE_SECONDS",
    "Beat",
    "Idle",
    "Loop",
    "Pass",
    "Ports",
    "Timing",
    "Wait",
    "beaten",
    "bounded",
    "decide",
    "heard",
    "idled",
    "lost",
    "pass_due",
    "pass_line",
    "pass_reason",
    "passed",
    "perform_batched",
    "perform_fenced",
    "planned_pass",
    "run",
    "store_waiter",
]

BEAT_SECONDS = 10.0  # the leases amendment (2026-11-20): heartbeat 10 s, lease 60 s
LEASE_TTL_SECONDS = chair_kinds.LEASE_TTL_SECONDS
# A burst of writes (or the worker's own) makes one pass, not one per commit.
MIN_GAP_SECONDS = 30.0
# Past the tick's window between its last lease read and performing a housekeeping or watch action: seconds, since
# those kinds lead the tick's perform. One beat interval on either side of it. A quarantine or decompose action can
# come later in the tick's perform (after a land); the tick's per-action lease check (`chair_exec.Deps.queue_held`)
# covers it there.
SETTLE_SECONDS = 30.0
# A lease call that has not answered by then is a lost lease, never a held one.
CALL_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class Timing:
    poll: float  # the fallback: a pass at least this often with no notification
    min_gap: float = MIN_GAP_SECONDS  # a notified pass waits this long after the last one
    beat: float = BEAT_SECONDS  # the lease is renewed (or retried) this often, never more often
    settle: float = SETTLE_SECONDS  # a freshly taken lease waits this long before its first pass


@dataclass(frozen=True)
class Loop:
    """What the loop remembers between steps, on the monotonic clock."""

    last_pass: float | None = None  # None until the first pass under the current lease
    woke: frozenset[str] = field(default_factory=frozenset[str])  # the kind's tables notified since the last pass
    last_beat: float | None = None  # the last acquire or renew attempt, held or not
    epoch: int | None = None  # the kind lease's epoch while held
    held_since: float | None = None  # when this epoch was taken


@dataclass(frozen=True)
class Idle:
    """The profile does not list the kind: let any lease go and look again after `wait`."""

    release: bool
    wait: float


@dataclass(frozen=True)
class Beat:
    """Acquire or renew the kind lease now."""


@dataclass(frozen=True)
class Pass:
    reason: str


@dataclass(frozen=True)
class Wait:
    seconds: float
    standby: bool  # True while another worker holds the kind


Step = Idle | Beat | Pass | Wait


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


def _until_beat(loop: Loop, now: float, timing: Timing) -> float:
    return timing.beat if loop.last_beat is None else max(0.0, timing.beat - (now - loop.last_beat))


def _until_pass(loop: Loop, now: float, timing: Timing) -> float:
    if loop.last_pass is None:
        return 0.0
    since = now - loop.last_pass
    return max(0.0, min(timing.poll - since, timing.min_gap - since if loop.woke else timing.poll - since))


def decide(loop: Loop, now: float, queued: bool, dry_run: bool, timing: Timing) -> Step:
    """Pure. The loop's next step.

    A live worker whose kind is not listed idles; one with a beat due beats; one without the lease waits out the rest
    of the beat (a standby never waits zero, so it cannot spin); one that has just taken the lease settles; then a pass
    when one is due, else a wait until the sooner of the next beat and the next pass. A dry run holds no lease."""
    if dry_run:
        return Pass(pass_reason(loop)) if pass_due(loop, now, timing) else Wait(_until_pass(loop, now, timing), False)
    if not queued:
        return Idle(release=loop.epoch is not None, wait=timing.beat)
    if loop.last_beat is None or now - loop.last_beat >= timing.beat:
        return Beat()
    if loop.epoch is None:
        return Wait(_until_beat(loop, now, timing), True)
    settle_left = timing.settle - (now - loop.held_since) if loop.held_since is not None else 0.0
    if settle_left > 0:
        return Wait(min(settle_left, _until_beat(loop, now, timing)), False)
    if pass_due(loop, now, timing):
        return Pass(pass_reason(loop))
    return Wait(min(_until_beat(loop, now, timing), _until_pass(loop, now, timing)), False)


def beaten(loop: Loop, now: float, epoch: int | None) -> Loop:
    """Pure. The loop after a lease attempt at `now` that held `epoch` (None: refused, failed or timed out). A new
    epoch starts its own settle and its own first pass."""
    if epoch is None:
        return replace(loop, last_beat=now, epoch=None, held_since=None)
    if epoch != loop.epoch:
        return replace(loop, last_beat=now, epoch=epoch, held_since=now, last_pass=None)
    return replace(loop, last_beat=now)


def passed(loop: Loop, now: float) -> Loop:
    """Pure. The loop after a pass that ended at `now`."""
    return replace(loop, last_pass=now, woke=frozenset())


def idled(loop: Loop) -> Loop:
    """Pure. The loop after an idle step released any lease: the next listed step beats at once."""
    return replace(loop, epoch=None, held_since=None, last_beat=None)


def lost(loop: Loop) -> Loop:
    """Pure. The lease was lost during a pass: stand by until the next beat."""
    return replace(loop, epoch=None, held_since=None)


def planned_pass(kind: str, facts: Facts, now: datetime, epoch: int, dry_run: bool) -> list[Action] | None:
    """Pure. The actions of one pass, each stamped with the kind lease's epoch; None when the facts say the setting
    does not list the kind (a live pass never plans a kind the tick may still be planning). A dry run plans anyway."""
    if not dry_run and not chair_kinds.worker_acts(kind, facts):
        return None
    # The queue alarms for the other kinds, once no tick raises them (C6, `chair_kinds.watcher`). A dry run holds no
    # lease and so watches no one: its plan stays the kind's own, which is what §5.3's comparison reads.
    alarms = [] if dry_run else chair_kinds.watcher_actions(kind, facts)
    return [stamp(a, epoch) for a in [*plan_kind(kind, facts, now), *alarms]]


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


def perform_fenced(
    actions: Sequence[Action], holds: Callable[[], bool], perform_one: Callable[[Action], list[Result]]
) -> list[Result]:
    """Edge. Each action in order, each only after the store says the lease still holds; once it does not, that action
    and every later one are fenced and none of them runs."""
    results: list[Result] = []
    for i, action in enumerate(actions):
        if not holds():
            return [*results, *(make_result(a, "fenced", "the kind lease was lost") for a in actions[i:])]
        results.extend(perform_one(action))
    return results


def perform_batched(
    actions: Sequence[Action], holds: Callable[[], bool], epoch: Callable[[], int | None],
    perform_batch: Callable[[Sequence[Action], Callable[[], int]], list[Result]],
) -> list[Result]:
    """Edge. The whole pass in one perform (a batched kind, `chair_kinds.BATCHED_KINDS`), so the rules that span its
    actions hold as they do in the tick's perform: a relaunch is skipped when its branch clear did not finish, launches
    on one host are staggered. The perform reads the epoch before each action, and here that read asks the store
    whether the lease still holds: when it does not, the epoch reads as none held (-2) and that action, stamped with
    the lease's epoch, is fenced. Each action asks again: a lease taken over stays lost, so every later action is
    fenced too, but a store read that fails only briefly (`holds` is bounded, and a failure reads as not held)
    fences only the actions read while it failed, and the ones after it run once the store answers again."""

    def current() -> int:
        held = epoch()
        return held if held is not None and holds() else -2

    return perform_batch(actions, current)


def bounded[T](call: Callable[[], T], seconds: float, otherwise: T) -> T:
    """Edge. `call()`, or `otherwise` when it raises or has not returned within `seconds`. A call that hangs is left on
    its daemon thread; its late answer is dropped."""
    box: list[T] = []
    done = threading.Event()

    def work() -> None:
        try:
            box.append(call())
        except Exception:
            pass
        finally:
            done.set()

    threading.Thread(target=work, daemon=True).start()
    return box[0] if done.wait(seconds) and box else otherwise


@dataclass(frozen=True)
class Ports:
    """The worker's edges. `acquire` returns the lease's epoch or None; `renew` and `holds` whether the epoch still
    holds; each already bounded, so a failure or a hang reads as not held. `wait` returns the tables notified within
    its timeout; `pause` closes the listener for the length of a pass."""

    queued: Callable[[], frozenset[str]]
    acquire: Callable[[], int | None]
    renew: Callable[[int], bool]
    holds: Callable[[int], bool]
    release: Callable[[int], None]
    gather: Callable[[datetime], Facts]
    perform_one: Callable[[Action, Callable[[], int]], list[Result]]
    wait: Callable[[float], frozenset[str]]
    pause: Callable[[], None]
    clock: Callable[[], float]
    now: Callable[[], datetime]
    echo: Callable[[str], None]
    # A batched kind's whole pass in one perform (`perform_batched`); None performs it one action at a time.
    perform_batch: Callable[[Sequence[Action], Callable[[], int]], list[Result]] | None = None
    # The pass's push (C6): handed the kind, the facts and the pass's actions after they were performed, it pushes what
    # needs the operator and returns "" or a note for the pass line. None pushes nothing (the tick still does).
    notify: Callable[[str, Facts, Sequence[Action]], str] | None = None


@dataclass
class Held:
    """Edge state: the kind lease's epoch while held, shared with the pass's beat thread, the fence and the recorder."""

    epoch: int | None = None


def _pass(kind: str, ports: Ports, held: Held, reason: str, dry_run: bool, beat: float) -> str:
    """Edge. One pass: beat the lease for its whole length, gather, plan, perform each action behind the fence."""
    stop = threading.Event()
    if not dry_run:
        beat_in_thread(lambda: _renew_or_drop(ports, held, stop), beat, stop, lambda _exc: _drop(held, stop))
    try:
        now = ports.now()
        facts = ports.gather(now)
        epoch = held.epoch if held.epoch is not None and not dry_run else -1
        actions = planned_pass(kind, facts, now, epoch, dry_run)
        if not actions:
            return _with_note(pass_line(kind, reason, actions, [], dry_run, now), _push(kind, ports, facts, actions, dry_run))
        current = (lambda: -1) if dry_run else (lambda: held.epoch if held.epoch is not None else -2)

        def holds() -> bool:
            return dry_run or (held.epoch is not None and ports.holds(held.epoch))

        if kind in chair_kinds.BATCHED_KINDS and ports.perform_batch is not None:
            epoch_now = (lambda: -1) if dry_run else (lambda: held.epoch)
            results = perform_batched(actions, holds, epoch_now, ports.perform_batch)
        else:
            results = perform_fenced(actions, holds, lambda a: ports.perform_one(a, current))
        line = pass_line(kind, reason, actions, results, dry_run, now)
        line = f"{line} | lease lost: the rest fenced" if any(r["status"] == "fenced" for r in results) else line
        return _with_note(line, _push(kind, ports, facts, actions, dry_run))
    finally:
        stop.set()


def _push(kind: str, ports: Ports, facts: Facts, actions: Sequence[Action] | None, dry_run: bool) -> str:
    """Edge. The pass's push, on every live pass of a listed kind (a pass with nothing planned too, so a hold that
    cleared is seen to clear); never on a dry run or an unlisted kind. A push that raises is a note, not a failed pass."""
    if dry_run or actions is None or ports.notify is None:
        return ""
    try:
        return ports.notify(kind, facts, actions)
    except Exception as exc:
        return f"notify failed: {type(exc).__name__}: {exc}"


def _with_note(line: str, note: str) -> str:
    return f"{line} | {note}" if note else line


def _renew_or_drop(ports: Ports, held: Held, stop: threading.Event) -> None:
    """Edge. The beat during a pass: a renew refused, failed or timed out drops the epoch, so the rest is fenced."""
    epoch = held.epoch
    if epoch is not None and not ports.renew(epoch):
        _drop(held, stop)


def _drop(held: Held, stop: threading.Event) -> None:
    held.epoch = None
    stop.set()


def _beat(kind: str, ports: Ports, held: Held, loop: Loop, now: float) -> tuple[Loop, str]:
    """Edge. Renew the lease held, else try to take it; returns the folded loop and a line for a change of standing."""
    epoch = loop.epoch if loop.epoch is not None and ports.renew(loop.epoch) else ports.acquire()
    held.epoch = epoch
    after = beaten(loop, now, epoch)
    if epoch is not None and epoch != loop.epoch:
        return after, f"chair worker {kind} | holds {chair_kinds.lease_name(kind)} at epoch {epoch}"
    if epoch is None and loop.epoch is not None:
        return after, f"chair worker {kind} | lost {chair_kinds.lease_name(kind)}; standing by"
    return after, ""


def run(kind: str, ports: Ports, timing: Timing, once: bool, dry_run: bool, held: Held | None = None) -> int:
    """Edge. The worker loop until interrupted (KeyboardInterrupt, which SIGTERM raises), or one pass with `once`.

    Returns 0, or 1 when `once` could not act: the setting does not list the kind or another holder has its lease.
    The lease is released on every way out. `held` is the epoch cell the caller's recorder and fence also read."""
    if kind not in chair_kinds.QUEUE_KINDS:
        ports.echo(f"chair worker: no queue worker for kind {kind!r}; one of: {', '.join(chair_kinds.QUEUE_KINDS)}")
        return 2
    held = held if held is not None else Held()
    loop = Loop()
    said = ""  # the last standing line printed, so an idle or a standby is said once, not every beat

    def say_once(line: str) -> None:
        nonlocal said
        if line != said:
            ports.echo(line)
            said = line

    try:
        while True:
            now = ports.clock()
            step = decide(loop, now, dry_run or kind in ports.queued(), dry_run, timing)
            if isinstance(step, Idle):
                if step.release and loop.epoch is not None:
                    with contextlib.suppress(Exception):
                        ports.release(loop.epoch)
                held.epoch, loop = None, idled(loop)
                say_once(f"chair worker {kind} | idle: chair.queues does not list {kind}; the tick plans it")
                if once:
                    return 1
                loop = heard(loop, kind, ports.wait(step.wait))
            elif isinstance(step, Beat):
                loop, line = _beat(kind, ports, held, loop, now)
                if line:
                    ports.echo(line)
                    said = ""
            elif isinstance(step, Wait):
                if step.standby:
                    say_once(f"chair worker {kind} | standby: another worker holds {chair_kinds.lease_name(kind)}")
                    if once:
                        return 1
                loop = heard(loop, kind, ports.wait(step.seconds))
            else:
                ports.pause()
                try:
                    ports.echo(_pass(kind, ports, held, step.reason, dry_run, timing.beat))
                except Exception as exc:  # one bad pass must not stop the worker; the next is due on the poll
                    ports.echo(f"chair worker {kind} | {step.reason} | error: {type(exc).__name__}: {exc}")
                loop = passed(loop, ports.clock())
                if held.epoch is None and not dry_run:
                    loop = lost(loop)
                if once:
                    return 0
    except KeyboardInterrupt:
        return 0
    finally:
        if held.epoch is not None:
            with contextlib.suppress(Exception):
                ports.release(held.epoch)


def store_waiter(
    url: str, sleep: Callable[[float], None]
) -> tuple[Callable[[float], frozenset[str]], Callable[[], None]]:
    """Edge. A `wait` that listens for `cox_store` notifications on the store at `url` (`harness.store_notify`, an
    autocommit connection that drains everything already arrived on each wake), and `close`, which the loop also
    calls before every pass so no listener sits undrained through a long action.

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
