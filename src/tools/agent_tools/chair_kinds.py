"""Which of the chair's kinds of work a queue worker runs, and which the old tick loop still plans (C3).

Pure. The profile key `chair.queues` lists the kinds that have moved to their own worker process
(`cox chair worker --kind K`); the default is empty, so the tick plans every kind as before. Today the kinds that can
move are `housekeeping` and `watch`; the rest stay on the tick until C4 and C5.

Exactly one of the two plans a kind at any moment, and both decide it from the same facts:

- the tick skips a kind that the setting lists, and also one whose worker lease (`chair:<kind>`) is live, so a worker
  on a machine whose profile lists the kind is not doubled by a tick on a machine whose profile does not;
- a worker acts only for a kind the setting lists, and only while it holds that kind's lease, so two copies of one
  kind never act at once.

A worker wakes on a `cox_store` notification (migration 0021) for a table its kind reads, and polls on a timer as its
fallback, so a missed notification costs latency and never a wrong decision.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import cast

from agent_tools.chair_types import Facts

__all__ = [
    "LEASE_PREFIX",
    "POLL_SECONDS",
    "QUEUE_KINDS",
    "WAKE_TABLES",
    "lease_name",
    "live_kinds",
    "queued_kinds",
    "tick_skips",
    "worker_acts",
]

QUEUE_KINDS = ("housekeeping", "watch")

# The tables each kind's facts are read from; a notification for one of them is a reason to plan again now. The watch
# reads a land's main CI from the forge, which no table records, so the watch leans on its poll more than housekeeping.
WAKE_TABLES: Mapping[str, frozenset[str]] = {
    "housekeeping": frozenset({"work_items", "runs", "hosts", "workers", "phase_queue", "step_results"}),
    "watch": frozenset({"landing_records", "land_queue"}),
}

# The fallback: a worker plans at least this often with no notification at all (the tick's own default interval).
POLL_SECONDS: Mapping[str, float] = {"housekeeping": 60.0, "watch": 60.0}

LEASE_PREFIX = "chair:"


def lease_name(kind: str) -> str:
    """The store lease one worker of `kind` holds: `chair:housekeeping`. Not the chair's own lease, `chair`."""
    return f"{LEASE_PREFIX}{kind}"


def queued_kinds(raw: object) -> frozenset[str]:
    """The kinds `chair.queues` moves to a worker. Anything but a list of known kind names counts as none: a typo
    leaves the kind on the tick, where it has always run, rather than with no planner at all."""
    if not isinstance(raw, list | tuple):
        return frozenset()
    items = cast(Sequence[object], raw)
    return frozenset(k for k in items if isinstance(k, str) and k in QUEUE_KINDS)


def live_kinds(leases: Mapping[str, tuple[str, str, str]], now: str) -> frozenset[str]:
    """The kinds whose worker lease is unexpired at `now`. `leases` maps a lease name to (holder, expires_at,
    heartbeat_at), as `run_store.lease_table_for` returns it; times are `YYYY-MM-DDTHH:MM:SSZ`, which sort as text."""
    return frozenset(
        name.removeprefix(LEASE_PREFIX)
        for name, (_holder, expires_at, _beat) in leases.items()
        if name.startswith(LEASE_PREFIX) and name.removeprefix(LEASE_PREFIX) in QUEUE_KINDS and str(expires_at) > now
    )


def tick_skips(facts: Facts) -> frozenset[str]:
    """The kinds the tick does not plan: those the setting lists and those a live worker holds."""
    return queued_kinds(list(facts.get("chair_queues") or ())) | frozenset(_known(facts.get("queue_workers") or ()))


def worker_acts(kind: str, facts: Facts) -> bool:
    """Whether a worker of `kind` that holds its lease plans and acts: only for a kind the setting lists."""
    return kind in queued_kinds(list(facts.get("chair_queues") or ()))


def _known(kinds: Iterable[str]) -> Collection[str]:
    return [k for k in kinds if k in QUEUE_KINDS]
