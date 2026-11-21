"""Which of the chair's kinds of work a queue worker runs, and which the old tick loop still plans (C3).

Pure. The profile key `chair.queues` lists the kinds that have moved to their own worker process
(`cox chair worker --kind K`); the default is empty, so the tick plans every kind as before. The kinds that can move
are `housekeeping` and `watch` (C3), `quarantine` and `decompose` (C4), and `launch` (C5).

`launch` is the single owner of every run the chair starts (C5): the old model's launch_epic, relaunch, retry, rescue
and decompose launches, and the phase queue's `queue_phases`. Whoever holds the launch slot (the tick, or the one
worker holding `chair:launch`) plans the launch cap from one set of facts and performs it in one process, so two
launches never race for the last lane: there is no second launcher whose plan could have read the same free lane.

Who plans a kind, decided from the same facts on both sides:

- the tick skips a kind that its profile lists, and a kind whose worker lease (`chair:<kind>`) is live by the store's
  clock whatever its profile says, so a worker on a machine whose profile lists the kind is never doubled by a tick
  on one whose profile does not;
- when the leases cannot be read and a worker could be running (a kind is listed, or a worker lease has been seen),
  the tick skips every kind a worker could run (it fails closed), and says so once the read has failed for longer
  than the lease's time to live; on the default, with no worker in sight, a failed read changes nothing;
- a worker acts only for a kind its profile lists, and only while it holds that kind's lease;
- a kind the profile lists whose lease has been dead for longer than its time to live has no owner: the tick says so
  with one needs_chair (`unowned_actions`), which the notifier pushes once while it stands. Once no tick holds the
  chair lease (after the cutover, C6), the first live worker in `QUEUE_KINDS` order says it for every other kind
  (`watcher`), so a dead worker, the housekeeping one included, is always reported by someone.

The facts carry the leases as `queue_leases` (kind to `expires_at`, None when unreadable), the store's clock at that
read as `queue_lease_now` (so every comparison is on the store's clock, never a machine's), whether a worker lease row
has ever been seen (`queue_leases_seen`), and how long the read has been failing (`queue_leases_failing_s`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from agent_tools.chair_types import Action, ActionKind, Facts
from core.jsontypes import is_mapping

__all__ = [
    "BATCHED_KINDS",
    "LAUNCH_LEASES",
    "LEASE_PREFIX",
    "LEASE_TTL_SECONDS",
    "POLL_SECONDS",
    "QUEUE_KINDS",
    "UNOWNED_CAUSE",
    "UNREADABLE_CAUSE",
    "WAKE_TABLES",
    "QueueProof",
    "launch_refusal",
    "lease_name",
    "live_kinds",
    "proof_argv",
    "queued_kinds",
    "tick_skips",
    "tick_watching",
    "unowned_actions",
    "unowned_kinds",
    "unreadable_actions",
    "watcher",
    "watcher_actions",
    "worker_acts",
]

QUEUE_KINDS = ("housekeeping", "watch", "quarantine", "decompose", "launch")

# The tables whose write is a reason to plan a kind again now. Housekeeping reads hosts and workers too, but a host or
# worker beat writes them every few seconds across a fleet, and each wake costs a full gather (with forge calls), so
# those changes wait for the poll. A wake is at most one pass per `min_gap`, so a kind passes at most every
# `min_gap` seconds however busy its tables are, and at least every `poll` seconds when they are quiet.
WAKE_TABLES: Mapping[str, frozenset[str]] = {
    "housekeeping": frozenset({"work_items", "runs"}),
    "watch": frozenset({"landing_records", "land_queue"}),
    # A phase that finishes moves its `phase_queue` row (state, outcome) and its tasks' `work_items` rows; either wakes
    # the triage. On the phase model that move is the event, so no `run_exited` courier line is needed.
    "quarantine": frozenset({"work_items", "runs", "phase_queue"}),
    # A new intake is a `work_items` row; a decompose run's exit is a `runs` row.
    "decompose": frozenset({"work_items", "runs", "phase_queue"}),
    # A launch is due when work becomes ready (`work_items`), a lane frees (`runs`), or a phase row moves (`phase_queue`).
    "launch": frozenset({"work_items", "runs", "phase_queue"}),
}

# The kinds whose pass is performed as one batch (`chair_queue.Ports.perform_batch`), each action still behind the
# store's lease check: a launch pass carries rules across its actions (a relaunch waits on its branch clear, launches
# on one host are staggered), which one action at a time would lose.
BATCHED_KINDS = frozenset({"launch"})

# The fallback: a worker plans at least this often with no notification at all (the tick's own default interval).
POLL_SECONDS: Mapping[str, float] = dict.fromkeys(QUEUE_KINDS, 60.0)

# The leases amendment (2026-11-20): heartbeat 10 s, lease 60 s.
LEASE_TTL_SECONDS = 60

LEASE_PREFIX = "chair:"
UNOWNED_CAUSE = "queue_unowned"
UNREADABLE_CAUSE = "queue_leases_unreadable"


def lease_name(kind: str) -> str:
    """The store lease one worker of `kind` holds: `chair:housekeeping`. Not the chair's own lease, `chair`."""
    return f"{LEASE_PREFIX}{kind}"


def queued_kinds(raw: object) -> frozenset[str]:
    """The kinds `chair.queues` moves to a worker. Anything but a list of known kind names counts as none: a name no
    worker runs stays on the tick, where it has always run, rather than with no planner at all."""
    if not isinstance(raw, list | tuple):
        return frozenset()
    items = cast(Sequence[object], raw)
    return frozenset(k for k in items if isinstance(k, str) and k in QUEUE_KINDS)


def _at(stamp: str) -> datetime | None:
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def live_kinds(leases: Mapping[str, str], now: str) -> frozenset[str]:
    """The kinds whose worker lease is unexpired at `now`. `leases` maps a kind to its lease's `expires_at`; both
    times are the store's `YYYY-MM-DDTHH:MM:SSZ` stamps, which sort as text."""
    return frozenset(kind for kind, expires_at in leases.items() if kind in QUEUE_KINDS and str(expires_at) > now)


def _leases(facts: Facts) -> Mapping[str, str] | None:
    """The kinds' leases, or None when the read failed. Facts with no key at all read as no lease (a gather that never
    had the reader, as in the planner tests)."""
    if "queue_leases" not in facts:
        return {}
    return facts["queue_leases"]


def _workers_matter(facts: Facts) -> bool:
    """Whether a queue worker could be running: the profile lists a kind, or a worker lease row has been seen."""
    return bool(queued_kinds(list(facts.get("chair_queues") or ()))) or bool(facts.get("queue_leases_seen"))


def tick_skips(facts: Facts) -> frozenset[str]:
    """The kinds the tick does not plan: those its profile lists, those a live worker lease holds, and, when the leases
    could not be read and a worker could be running, every kind a worker could run. On the default (nothing listed, no
    worker lease ever seen) a failed read changes nothing."""
    queued = queued_kinds(list(facts.get("chair_queues") or ()))
    leases = _leases(facts)
    if leases is None:
        return frozenset(QUEUE_KINDS) if _workers_matter(facts) else queued
    return queued | live_kinds(leases, facts.get("queue_lease_now", ""))


def worker_acts(kind: str, facts: Facts) -> bool:
    """Whether a worker of `kind` that holds its lease plans and acts: only for a kind its profile lists."""
    return kind in queued_kinds(list(facts.get("chair_queues") or ()))


def unowned_kinds(facts: Facts, ttl: int = LEASE_TTL_SECONDS) -> frozenset[str]:
    """The listed kinds no worker has held for longer than `ttl` by the store's clock: no lease row at all, or one that
    expired more than `ttl` seconds ago. None when the leases could not be read (nothing is known)."""
    leases = _leases(facts)
    now = _at(facts.get("queue_lease_now", ""))
    if leases is None or now is None:
        return frozenset()
    queued = queued_kinds(list(facts.get("chair_queues") or ()))

    def dead(kind: str) -> bool:
        expired = _at(leases.get(kind, ""))
        return expired is None or (now - expired).total_seconds() > ttl

    return frozenset(k for k in queued if dead(k))


def unreadable_actions(facts: Facts, ttl: int = LEASE_TTL_SECONDS) -> list[Action]:
    """One needs_chair while the worker leases have been unreadable for longer than `ttl` and a worker could be running:
    the tick then leaves every kind alone (it fails closed), and a worker cannot tell whether it still holds its own, so
    nothing may be acting until the store answers. Pushed once while it stands, as `unowned_actions`. The tick's facts
    carry how long the read has failed; a worker's facts carry the failure but not its length, so a worker that sees
    one says so at once (`watcher_actions`)."""
    if _leases(facts) is not None or not _workers_matter(facts) or float(facts.get("queue_leases_failing_s", 0.0)) <= ttl:
        return []
    return [{
        "kind": ActionKind.NEEDS_CHAIR, "initiative": "chair-queue", "cause": UNREADABLE_CAUSE,
        "reason": (
            f"the worker leases (chair:<kind>) have not been readable for over {ttl}s, so the tick leaves "
            f"{', '.join(QUEUE_KINDS)} alone and a worker may not be acting either: check the store"
        ),
    }]


def unowned_actions(facts: Facts, exclude: frozenset[str] = frozenset()) -> list[Action]:
    """One needs_chair per listed kind with no live worker, but those in `exclude` (a worker never reports its own
    kind: it is running). It stands each pass, so the status line names it, and the notifier pushes its key once while
    it stands (`notify_core.due`)."""
    return [
        {
            # The lease as the task: the record's key is (ts, holder, kind, target) and the target is the task first,
            # so the alarms of one pass are a row each, not one row for whichever kind came first.
            "kind": ActionKind.NEEDS_CHAIR, "initiative": "chair-queue", "task_id": lease_name(kind),
            "cause": f"{UNOWNED_CAUSE} {kind}",
            "reason": (
                f"chair.queues lists {kind} but no worker has held {lease_name(kind)} for over {LEASE_TTL_SECONDS}s, so "
                f"nothing plans it: start `cox chair worker --kind {kind}`, or take {kind} out of chair.queues"
            ),
        }
        for kind in sorted(unowned_kinds(facts) - exclude)
    ]


def tick_watching(facts: Facts) -> bool:
    """Whether a tick holds the chair lease now: live, not released, and not a session's informal hold (a session plans
    no tick). While one does, the tick raises the queue alarms (`chair_plan.plan_tick_held`) and no worker raises
    them a second time. Facts without the chair lease read as no tick."""
    lease = facts.get("lease")
    if not is_mapping(lease):
        return False
    return not (lease.get("released", True) or lease.get("stale", True) or lease.get("session_held", False))


def watcher(kind: str, facts: Facts) -> bool:
    """Pure. Whether the worker of `kind`, holding its lease, raises the queue alarms this pass (C6).

    The queue alarms (`unowned_actions`, `unreadable_actions`) were the tick's alone, and the cutover stops the tick.
    So once no tick holds the chair lease, one worker raises them: the first kind of `QUEUE_KINDS` that is listed and
    either holds a live lease or is this worker (which holds its own while it passes). Housekeeping raises them while
    it runs. With housekeeping dead, the watch worker raises them, and its first alarm is that housekeeping has no
    owner; and so on down the list. The next live worker takes over from a dead watcher within one lease time to live,
    so no one dead process silences them. With the tick and every worker dead, the chair watchdog's timer still says
    so (`chair_watchdog.queues_down`).

    When the leases cannot be read no worker can tell which of them is first, so each one raises the alarm: a
    duplicate push is better than none."""
    if tick_watching(facts):
        return False
    queued = queued_kinds(list(facts.get("chair_queues") or ()))
    if kind not in queued:
        return False
    leases = _leases(facts)
    if leases is None:
        return True
    live = live_kinds(leases, facts.get("queue_lease_now", "")) | {kind}
    return next(k for k in QUEUE_KINDS if k in queued and k in live) == kind


def watcher_actions(kind: str, facts: Facts) -> list[Action]:
    """Pure. The queue alarms the worker of `kind` raises this pass: none unless it is the watcher (`watcher`). The
    watcher raises one `queue_unowned` per other listed kind with no live worker, and the unreadable alarm once the
    leases have failed to read for longer than their time to live (by the worker's own reader, as the tick's)."""
    if not watcher(kind, facts):
        return []
    return [*unowned_actions(facts, exclude=frozenset({kind})), *unreadable_actions(facts)]


# The launches a queue worker makes, by graph, and the kind lease each is made under. `cox route launch <graph>` started
# beside the loop's live lock (held under the tick's label) is accepted only while the store says that lease is held,
# by that holder at that epoch: a worker that lost its lease, or a stale process naming an old epoch, is refused.
# Every graph is the launch kind's (C5): a decompose launch is no longer the decompose worker's, so no second process
# can start a run against the same free lane.
LAUNCH_LEASES: Mapping[str, str] = {graph: lease_name("launch") for graph in ("decompose", "epic", "rescue")}


@dataclass(frozen=True)
class QueueProof:
    """The kind lease a queue worker's launch is made under: the lease's name, its holder and its epoch."""

    lease: str
    holder: str
    epoch: int


def proof_argv(proof: QueueProof) -> list[str]:
    """Pure. The `cox route launch` flags that carry `proof`."""
    return ["--queue-lease", proof.lease, "--queue-holder", proof.holder, "--queue-epoch", str(proof.epoch)]


def launch_refusal(refusal: str | None, graph: str, proof: QueueProof | None, held: bool) -> str | None:
    """Pure. Whether a launch goes on: None to go on, else the line it is refused with.

    `refusal` is the loop lock's own (`chair.guard`: another label's live lock), None when the lock lets it through.
    A launch the lock refuses goes on only with a proof of the kind lease its graph is made under (`LAUNCH_LEASES`)
    that the store says is `held`, by its holder at its epoch."""
    if refusal is None or proof is None:
        return refusal
    if LAUNCH_LEASES.get(graph) != proof.lease:
        return f"{refusal}; {proof.lease} does not cover a {graph} launch"
    if not held:
        return f"{refusal}; {proof.lease} is not held by {proof.holder} at epoch {proof.epoch} by the store's clock"
    return None
