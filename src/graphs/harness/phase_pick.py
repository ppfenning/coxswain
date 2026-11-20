"""The phase queue's rules, pure: which phase a worker claims, how long a claim lasts, when a worker must stop, whether
a write still holds its claim, and what a finished run means for its phase.

Nothing here reads a clock or a store. `now` is the store's own time as a stamp (`YYYY-MM-DDTHH:MM:SSZ`, which
compares in order as text), read once by `store_phases` inside the transaction that uses it; a worker's own clock is
only ever compared with itself (`must_stop` takes two readings of one monotonic clock).

The timings are the operator's: a heartbeat every 10 s, a lease of 60 s, and a worker that has gone 45 s without a
successful heartbeat stops itself, so its claim is never run by two workers at once (its lease outlives its last
chance to act by 15 s). A lapsed claim is simply claimable again; there is no sweeper.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

__all__ = [
    "CANDIDATES",
    "HEARTBEAT_S",
    "LEASE_S",
    "MAX_ATTEMPTS",
    "STOP_AFTER_S",
    "Verdict",
    "backoff_s",
    "bad_tokens",
    "claimable",
    "fenced",
    "lease",
    "meets",
    "must_stop",
    "pick",
    "stamp_of",
    "verdict",
]

HEARTBEAT_S = 10
LEASE_S = 60
STOP_AFTER_S = 45
CANDIDATES = 16  # rows one claim locks and chooses among, in (band, seq) order
MAX_ATTEMPTS = 3  # claims of one phase before a failure is permanent
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


def lease(now: str, ttl_s: int = LEASE_S) -> str:
    """The stamp `ttl_s` seconds after `now`: when a claim taken or renewed at `now` lapses."""
    return (_parse(now) + timedelta(seconds=ttl_s)).strftime(_STAMP)


def backoff_s(attempts: int) -> int:
    """Seconds a retried phase waits before it is claimable again: 60 s after the first attempt, doubling, at most 15 min."""
    return min(_BACKOFF_BASE_S * 2 ** max(attempts - 1, 0), _BACKOFF_CAP_S)


def must_stop(last_ok: float, now: float, stop_after_s: float = STOP_AFTER_S) -> bool:
    """Whether a worker whose last successful heartbeat was at `last_ok` must stop at `now`, both on its own monotonic
    clock: it stops before its lease can lapse, so a successor never overlaps it."""
    return now - last_ok >= stop_after_s


def bad_tokens(tokens: Iterable[str]) -> list[str]:
    """The tokens that are not `name` or `key:value` in lowercase (`docker`, `os:macos`, `repo:app`), sorted."""
    return sorted({t for t in tokens if not _TOKEN.fullmatch(t)})


def meets(requires: Collection[str], capabilities: Collection[str]) -> bool:
    """A worker may run a phase when every requirement is one of its capabilities. No requirement is met by anyone."""
    return set(requires) <= set(capabilities)


def _requires(row: Row) -> list[str]:
    raw = row.get("requires_json")
    return [str(t) for t in raw] if isinstance(raw, list) else []


def claimable(row: Row, now: str) -> bool:
    """A `ready` row whose `ready_at` has come (a retried phase waits out its backoff), or a `claimed` one whose lease
    lapsed before `now`: its worker stopped heartbeating."""
    state = row.get("state")
    if state == "ready":
        return str(row.get("ready_at") or "") <= now
    return state == "claimed" and str(row.get("lease_until") or "") < now


def _order(row: Row) -> tuple[int, int, str, str]:
    band, seq = row.get("band"), row.get("seq")
    return (
        band if isinstance(band, int) else 1, seq if isinstance(seq, int) else 0,
        str(row.get("initiative")), str(row.get("phase")),
    )


def pick(rows: Sequence[Row], capabilities: Collection[str], free: int, now: str) -> Row | None:
    """The first claimable row, in (band, seq) order, whose requirements the worker meets; None when it has no free
    lane or none qualifies. The candidates arrive already locked; the ones not picked unlock at commit."""
    if free <= 0:
        return None
    return next((r for r in sorted(rows, key=_order) if claimable(r, now) and meets(_requires(r), capabilities)), None)


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
    again after `wait_s`; `quarantine` finishes it with `outcome` quarantined (its tasks stopped) or failed (it ran
    out of attempts)."""

    kind: Kind
    outcome: str
    reason: str
    wait_s: int = 0


def verdict(task_states: Sequence[str], attempts: int, launch_error: str = "", max_attempts: int = MAX_ATTEMPTS) -> Verdict:
    """The ack or nack for a phase whose run ended, from its tasks' states after the run and the claims it has had.

    Ack: every task is settled and at least one was approved, which the store records only once its review verdict is
    approve. A permanent nack: no task is left to build and one stopped (quarantined or held), or the phase has had
    `max_attempts` claims. Anything else is retryable, with backoff; so is a run that never started (`launch_error`)."""
    states = set(task_states)
    if not launch_error and states and states <= _SETTLED_OK and states & {"approved", "done"}:
        return Verdict("ack", "approved", "every task of the phase is approved")
    if not launch_error and states & _STOPPED and not states & _OPEN:
        stopped = sorted(states & _STOPPED)
        return Verdict("quarantine", "quarantined", f"no task is left to build and some are {' and '.join(stopped)}")
    why = launch_error or (f"tasks left {', '.join(sorted(states))}" if states else "the phase has no tasks in the store")
    if attempts >= max_attempts:
        return Verdict("quarantine", "failed", f"{attempts} attempt(s), the last: {why}")
    return Verdict("retry", "", why, backoff_s(attempts))
