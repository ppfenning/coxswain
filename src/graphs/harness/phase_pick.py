"""The phase queue's rules, pure: which phase a worker claims, how long a claim lasts, when a worker must stop, whether
a write still holds its claim, and what a finished run means for its phase.

Nothing here reads a clock or a store. `now` is the store's own time as a stamp (`YYYY-MM-DDTHH:MM:SSZ`, which
compares in order as text), read once by `store_phases` inside the transaction that uses it; a worker's own clock is
only ever compared with itself (`next_step` takes readings of one monotonic clock).

The timings are the operator's: a heartbeat every 10 s, a lease of 60 s, and a worker that has gone 45 s without a
successful heartbeat has stopped what it runs for that claim: it starts stopping early enough (`stop_at`) that the
TERM grace and the KILL wait end by then, and it never starts a heartbeat that could run past that point
(`next_step`). Its clock starts before the claim is taken, so the run is gone 15 s before the lease it was given can
lapse: no phase is run by two workers at once. A lapsed claim is simply
claimable again; there is no sweeper. A phase that has had `MAX_ATTEMPTS` claims is never claimed again: the chair
finishes it as failed (`exhausted`).

The operator may give a worker a shorter lease (`cox worker run --lease-s N`, `Timings.of`): the heartbeat, the stop
time and the lane's margin scale with it, a sixth, three quarters and a quarter of it, as 10, 45 and 15 are of 60. A
claim carries its own lease's length (`lease_until` less `heartbeat_at`), so the lane reads its margin from the claim
row (`margin_of`) and nothing has to pass the length to it.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

__all__ = [
    "BEAT_LOOP_S",
    "CANDIDATES",
    "DEFAULT_TIMINGS",
    "HEARTBEAT_CALL_S",
    "HEARTBEAT_S",
    "KILL_WAIT_S",
    "LANE_MARGIN_S",
    "LANE_STALL_S",
    "LEASE_S",
    "MAX_ATTEMPTS",
    "MIN_LEASE_S",
    "PUSH_BOUND_S",
    "RUN_LOST",
    "STOP_AFTER_S",
    "STOP_GRACE_S",
    "SmokeSeen",
    "Timings",
    "Verdict",
    "backoff_s",
    "bad_tokens",
    "choices",
    "claimable",
    "exhausted",
    "fenced",
    "lane_stalled",
    "lane_verdict",
    "lapsed",
    "lease",
    "lease_problem",
    "lost_reason",
    "margin_of",
    "meets",
    "next_step",
    "refusal",
    "seconds",
    "stamp_of",
    "stop_at",
    "verdict",
]

HEARTBEAT_S = 10
LEASE_S = 60
STOP_AFTER_S = 45
HEARTBEAT_CALL_S = 10  # the longest one heartbeat call can take: a 5 s connect plus a 5 s statement (`worker.bounded_url`)
STOP_GRACE_S = 5  # TERM to KILL, for the launch and the harness alike
KILL_WAIT_S = 2  # KILL to gone
BEAT_LOOP_S = 1  # how often the heartbeat loop looks at its claims
CANDIDATES = 16  # rows one page of a claim reads, in (band, seq) order; it pages on past rows this worker cannot meet
MAX_ATTEMPTS = 3  # claims of one phase before a failure is permanent
# A lane stops itself once its claim's lease has less than this left by the store's clock: its worker has gone the
# 45 s it may go without a heartbeat, so the lane is gone before the lease can lapse and another lane be started.
LANE_MARGIN_S = LEASE_S - STOP_AFTER_S
# A lane rewrites its progress mark every 2 s while it holds its claim; one whose mark has not moved for this long,
# under a worker that is itself alive, is hung (stopped, deadlocked) and is stopped so its phase can be claimed again.
LANE_STALL_S = 90
# The lane's margin must hold one bounded push (`lane_rollup.REMOTE_TIMEOUT_S`): a push follows a check that found it.
PUSH_BOUND_S = 8
MIN_LEASE_S = 32  # the shortest lease `lease_problem` accepts: a quarter of it is the push bound
_BACKOFF_BASE_S = 60
_BACKOFF_CAP_S = 900

_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_TOKEN = re.compile(r"[a-z0-9][a-z0-9._-]*(:[a-z0-9._-]+)?")

type Row = Mapping[str, object]


def stamp_of(raw: str) -> str:
    """The store's clock text (`2026-11-20 10:00:00.123456`, or the same with a `T`) as a stamp, to the second."""
    text = raw.strip().replace("T", " ")[:19]
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).strftime(_STAMP)


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, _STAMP).replace(tzinfo=UTC)


def seconds(earlier: str, later: str) -> int:
    """The whole seconds from stamp `earlier` to stamp `later` (negative when `later` is the earlier); ValueError when
    either is not a stamp."""
    return int((_parse(later) - _parse(earlier)).total_seconds())


def lease(now: str, ttl_s: int = LEASE_S) -> str:
    """The stamp `ttl_s` seconds after `now`: when a claim taken or renewed at `now` lapses."""
    return (_parse(now) + timedelta(seconds=ttl_s)).strftime(_STAMP)


def backoff_s(attempts: int) -> int:
    """Seconds a retried phase waits before it is claimable again: 60 s after the first attempt, doubling, at most 15 min."""
    return min(_BACKOFF_BASE_S * 2 ** max(attempts - 1, 0), _BACKOFF_CAP_S)


@dataclass(frozen=True)
class Timings:
    """One worker's lease and what follows from it: how often it renews, how long it may go without a good heartbeat
    before its run is gone, and the margin its lanes stop at. The default is the operator's 60 s."""

    lease_s: int = LEASE_S
    heartbeat_s: int = HEARTBEAT_S
    stop_after_s: int = STOP_AFTER_S

    @property
    def margin_s(self) -> int:
        """How much of the lease a lane must still have: once less is left, its worker has gone `stop_after_s`."""
        return self.lease_s - self.stop_after_s

    @classmethod
    def of(cls, lease_s: int) -> Timings:
        """The timings of a `lease_s` lease, scaled as 60 s's are: a heartbeat every sixth, a stop at three quarters."""
        return cls(lease_s, max(lease_s // 6, 1), lease_s - lease_s // 4)


DEFAULT_TIMINGS = Timings()


def lease_problem(lease_s: int) -> str:
    """Why a worker may not run on a `lease_s` lease, or "" when it may: its lanes' margin must hold one bounded push,
    and a heartbeat sent when due must be able to finish before the worker's stop time (`next_step`)."""
    t = Timings.of(lease_s)
    if t.margin_s < PUSH_BOUND_S:
        return f"a {lease_s}s lease leaves its lanes a {t.margin_s}s margin, less than one bounded push ({PUSH_BOUND_S}s)"
    if t.heartbeat_s + HEARTBEAT_CALL_S > stop_at(0.0, t):
        return f"a {lease_s}s lease leaves no time for a heartbeat ({HEARTBEAT_CALL_S}s) before the worker must stop"
    return ""


def margin_of(row: Row | None) -> int:
    """The margin a lane of this claim stops at: a quarter of the claim's own lease (`lease_until` less `heartbeat_at`,
    both written from one reading of the store's clock), or `LANE_MARGIN_S` when the row does not say one a worker
    accepts (`lease_problem`)."""
    try:
        ttl = seconds(str((row or {}).get("heartbeat_at")), str((row or {}).get("lease_until")))
    except ValueError:
        return LANE_MARGIN_S
    return Timings.of(ttl).margin_s if not lease_problem(ttl) else LANE_MARGIN_S


def stop_at(last_ok: float, timings: Timings = DEFAULT_TIMINGS) -> float:
    """When, on the worker's own monotonic clock, it must start stopping a claim whose last good heartbeat (or, before
    the first, the moment it set out to claim) was `last_ok`: early enough that the TERM grace, the KILL wait and one
    pass of the heartbeat loop all fit before `stop_after_s`, so the run is gone by then, `margin_s` inside the lease."""
    return last_ok + timings.stop_after_s - STOP_GRACE_S - KILL_WAIT_S - BEAT_LOOP_S


Step = Literal["wait", "beat", "stop"]


def next_step(last_ok: float, now: float, due: float, timings: Timings = DEFAULT_TIMINGS) -> Step:
    """What the heartbeat loop does for one claim at `now`: `stop` once `stop_at` has come, or when the heartbeat that
    is due could not finish before it (one call takes up to `HEARTBEAT_CALL_S`); `beat` when one is due and fits;
    else `wait`. Pure: both clocks are the worker's own monotonic clock."""
    deadline = stop_at(last_ok, timings)
    if now >= deadline:
        return "stop"
    if now < due:
        return "wait"
    return "stop" if now + HEARTBEAT_CALL_S > deadline else "beat"


def bad_tokens(tokens: Iterable[str]) -> list[str]:
    """The tokens that are not `name` or `key:value` in lowercase (`docker`, `os:macos`, `repo:app`), sorted."""
    return sorted({t for t in tokens if not _TOKEN.fullmatch(t)})


def meets(requires: Collection[str], capabilities: Collection[str]) -> bool:
    """A worker may run a phase when every requirement is one of its capabilities. No requirement is met by anyone."""
    return set(requires) <= set(capabilities)


def _requires(row: Row) -> list[str]:
    raw = row.get("requires_json")
    return [str(t) for t in raw] if isinstance(raw, list) else []


def _attempts(row: Row) -> int:
    raw = row.get("attempts")
    return raw if isinstance(raw, int) else 0


def _lapsed(row: Row, now: str) -> bool:
    return row.get("state") == "claimed" and str(row.get("lease_until") or "") < now


def lapsed(row: Row, now: str) -> bool:
    """A claimed row whose lease lapsed before `now`: its worker stopped renewing it, and nobody has claimed it since."""
    return _lapsed(row, now)


RUN_LOST = "run lost"  # how the chair's mark on a lapsed claim opens (`lost_reason`)


def lost_reason(row: Row) -> str:
    """The chair's "run lost" mark for a lapsed claim, as the row's reason: whose claim, its epoch, when it lapsed and
    the run it started. The same row always gives the same text, so the mark is written once."""
    run = str(row.get("run_id") or "")
    return (f"{RUN_LOST}: {row.get('claimed_by')}'s claim at epoch {row.get('claim_epoch')} lapsed at "
            f"{row.get('lease_until')}" + (f", leaving run {run}" if run else "") + "; claimable again")


def claimable(row: Row, now: str, max_attempts: int = MAX_ATTEMPTS) -> bool:
    """A `ready` row whose `ready_at` has come (a retried phase waits out its backoff), or a `claimed` one whose lease
    lapsed before `now`: its worker stopped heartbeating. Never one that has had `max_attempts` claims."""
    if _attempts(row) >= max_attempts:
        return False
    if row.get("state") == "ready":
        return str(row.get("ready_at") or "") <= now
    return _lapsed(row, now)


def exhausted(row: Row, now: str, max_attempts: int = MAX_ATTEMPTS) -> bool:
    """A row whose claims ran out while nobody holds it: `max_attempts` claims, and it is ready or its last claim
    lapsed. Its worker kept losing the lease, so no verdict was written; the chair finishes it as failed."""
    return _attempts(row) >= max_attempts and (row.get("state") == "ready" or _lapsed(row, now))


def _order(row: Row) -> tuple[int, int, str, str]:
    band, seq = row.get("band"), row.get("seq")
    return (
        band if isinstance(band, int) else 1, seq if isinstance(seq, int) else 0,
        str(row.get("initiative")), str(row.get("phase")),
    )


def choices(rows: Sequence[Row], capabilities: Collection[str], free: int, now: str) -> list[Row]:
    """The rows this worker may claim, in (band, seq) order: claimable, with every requirement met; none without a
    free lane. The claimer tries them in order and locks one at a time, so a row another worker holds is skipped, not
    waited on, and rows no one here can meet never stand in front of one it can."""
    if free <= 0:
        return []
    return [r for r in sorted(rows, key=_order) if claimable(r, now) and meets(_requires(r), capabilities)]


def fenced(row: Row | None, worker: str, epoch: int, now: str) -> bool:
    """Whether a write by `worker` under `epoch` still holds the claim at `now`: the row is claimed by it, at that
    epoch, and its lease has not lapsed. Every worker write is conditional on exactly this, so a stale worker's write
    matches no row."""
    return (
        row is not None and row.get("state") == "claimed" and row.get("claimed_by") == worker
        and row.get("claim_epoch") == epoch and str(row.get("lease_until") or "") > now
    )


Kind = Literal["ack", "retry", "quarantine"]

_SETTLED_OK = frozenset({"approved", "done", "dropped"})
_OPEN = frozenset({"ready", "in_progress", "todo"})
_STOPPED = frozenset({"quarantined", "blocked"})


@dataclass(frozen=True)
class Verdict:
    """What a finished run means for its phase. `ack` finishes it approved; `retry` releases the claim to be claimed
    again after `wait_s`, and with `refund` gives its attempt back (the run never started through no fault of the
    phase); `quarantine` finishes it with `outcome` quarantined (its tasks stopped) or failed (out of attempts)."""

    kind: Kind
    outcome: str
    reason: str
    wait_s: int = 0
    refund: bool = False


def refusal(reason: str, attempts: int) -> Verdict:
    """A launch the gates refused (the budget, the label guard, a live run of the phase), or a worker told to stop:
    the claim goes back without costing an attempt, after the backoff the attempts before it earned."""
    return Verdict("retry", "", reason, backoff_s(max(attempts - 1, 1)), refund=True)


def verdict(task_states: Sequence[str], attempts: int, launch_error: str = "", max_attempts: int = MAX_ATTEMPTS) -> Verdict:
    """The ack or nack for a phase whose run ended, from its tasks' states after the run and the claims it has had.

    Ack: every task is settled: approved or done (the store records approved only once the review verdict is
    approve), or dropped, so a phase whose tasks were all dropped has nothing left to build. A permanent nack: no
    task is left to build and one stopped (quarantined or held), or the phase has had `max_attempts` claims. Anything
    else is retryable, with backoff; so is a run that failed to start (`launch_error`)."""
    states = set(task_states)
    if not launch_error and states and states <= _SETTLED_OK:
        built = bool(states & {"approved", "done"})
        return Verdict("ack", "approved", "every task of the phase is approved" if built else "every task of the phase was dropped")
    if not launch_error and states & _STOPPED and not states & _OPEN:
        stopped = sorted(states & _STOPPED)
        return Verdict("quarantine", "quarantined", f"no task is left to build and some are {' and '.join(stopped)}")
    why = launch_error or (f"tasks left {', '.join(sorted(states))}" if states else "the phase has no tasks in the store")
    if attempts >= max_attempts:
        return Verdict("quarantine", "failed", f"{attempts} attempt(s), the last: {why}")
    return Verdict("retry", "", why, backoff_s(attempts))


@dataclass(frozen=True)
class SmokeSeen:
    """The phase smoke an ended lane wrote last, as the worker reads it: whether it passed, why not, and how many of
    the phase's smokes at that tip were harness faults, this one included (0 when it is not one)."""

    ok: bool
    reason: str
    faults: int = 0


def lane_verdict(tasks: Collection[str], rolled_up: Collection[str], last_round: tuple[int, str] | None, attempts: int,
                 max_attempts: int = MAX_ATTEMPTS, *, smoke: SmokeSeen | None, reason: str = "") -> Verdict:
    """The ack or nack for a phase whose lane ended, from what the store holds for it: the phase's tasks (dropped ones
    left out), the tasks whose roll-up put a commit on the phase branch, its last review round and that round's
    `reason`, and the phase smoke the ended lane wrote, or None when it wrote none (`lane_smoke`).

    The review round decides, as the lane's review loop wrote it (`review_loop`): approve is the ack, quarantine the
    permanent nack, with the round's reason, which the chair hands the phase's tasks for triage. A round of an
    episode that is over (the chair's `reopened` row) decides nothing. A failed smoke means no review:
    - a real failure finishes the phase `failed` at once: a retry would run the same commands on the same tip;
    - a harness fault (a check that could not run) goes back to the queue once, and a second at that tip is `failed`.
    Every other end is retryable with backoff, and after `max_attempts` claims the phase has failed: a phase with
    tasks left, a lane that ended before its smoke ran, and one whose smoke passed but whose review loop did not finish
    (a review that decided nothing, a revision that did not roll up, a lane that stopped). Each such retry counts its
    claim, so a loop that never finishes is bounded; nothing is parked, since the lane reviews its own phase.

    No failed smoke is quarantined: the chair reopens a quarantined phase whose tasks are still ready, which would
    run the same smoke on the same tip without bound; `failed` stays finished, for the operator."""
    if last_round is not None and last_round[1] == "approve":
        return Verdict("ack", "approved", f"the phase review approved it in round {last_round[0]}")
    if last_round is not None and last_round[1] == "quarantine":
        return Verdict("quarantine", "quarantined", f"the phase review quarantined it in round {last_round[0]}"
                                                    + (f": {reason}" if reason else ""))
    missing = sorted(set(tasks) - set(rolled_up))
    if tasks and not missing and smoke is not None and smoke.ok:
        why = "every task rolled up and the smoke passed, but the lane ended before its review finished the phase"
        return Verdict("quarantine", "failed", f"{attempts} attempt(s), the last: {why}") if attempts >= max_attempts \
            else Verdict("retry", "", why, backoff_s(attempts))
    if tasks and not missing and smoke is not None and not smoke.faults:
        return Verdict("quarantine", "failed", smoke.reason)
    if tasks and not missing and smoke is not None and smoke.faults > 1:
        return Verdict("quarantine", "failed", f"the phase smoke could not run {smoke.faults} times at this tip, "
                                               f"the last: {smoke.reason}")
    if tasks and not missing:
        why = smoke.reason if smoke is not None else "every task rolled up, but the lane ended before the phase smoke ran"
    else:
        why = f"tasks not rolled up: {', '.join(missing)}" if tasks else "the phase has no tasks in the store"
    if attempts >= max_attempts:
        return Verdict("quarantine", "failed", f"{attempts} attempt(s), the last: {why}")
    return Verdict("retry", "", why, backoff_s(attempts))


def lane_stalled(seen: tuple[str, float] | None, mark: str, now: float, limit_s: float = LANE_STALL_S) -> tuple[tuple[str, float], bool]:
    """Pure. Follow a lane's progress mark: `seen` is the mark last read and when it last changed (None before the
    first read), `mark` what it reads now ("" while there is none), `now` the worker's own monotonic clock. The new
    `seen`, and whether the mark has stood still for `limit_s`: a lane that never wrote one counts from the first read."""
    if seen is None or seen[0] != mark:
        return (mark, now), False
    return seen, now - seen[1] >= limit_s
