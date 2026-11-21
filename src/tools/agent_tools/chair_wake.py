"""Chair wake: end the wait between ticks early when a run exits, intake arrives or an operator directive lands on the bus,
or, on a store with migration 0021's notifications, when a table the chair acts on is written (`STORE_WAKE_TABLES`)."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence

from agent_tools.courier import parse_run_exited

__all__ = ["STORE_WAKE_TABLES", "new_wake_events", "wait_for_events", "wait_for_run_exit"]

RUN_EXITED = "run_exited"
INTAKE = "intake"  # courier has no intake event: a plain entry, with no `event`, whose ref is `coxswain://intake/<id>`
CHAT_DIRECTIVE = "chat_directive"  # a courier event name, matched on the entry's `event`


# A phase the worker finished, approved or quarantined by its lane's review, is what the chair acts on next: it
# approves or quarantines the phase's tasks (`chair_phase_queue`). The lane's review round no longer wakes it: the
# lane reviews its own phase, so no parked phase waits on a round (L6). The chair's own `queue_phases` write wakes it
# once more too, and that tick plans nothing; a heartbeat wakes nobody (`store_ddl_0021.BEATS`).
STORE_WAKE_TABLES = frozenset({"phase_queue"})

Entry = Mapping[str, object]


def _wakes(entry: Entry, kind: str) -> bool:
    if kind == RUN_EXITED:
        return parse_run_exited(entry) is not None
    if kind == INTAKE:  # every entry carries a `ref`, so an event on an intake ref (an answer, an applied note) is not new intake
        return entry.get("event") is None and str(entry.get("ref", "")).startswith("coxswain://intake/")
    return entry.get("event") == kind


def _entry_id(entry: Entry) -> str | None:
    entry_id = entry.get("id")
    return entry_id if isinstance(entry_id, str) else None


def new_wake_events(entries: Sequence[Entry], seen_ids: Collection[str], kinds: Collection[str]) -> list[Entry]:
    """Entries of one of `kinds` that are unacked and whose id is not in `seen_ids`."""
    return [
        e for e in entries
        if not e.get("ack") and (entry_id := _entry_id(e)) is not None and entry_id not in seen_ids and any(_wakes(e, k) for k in kinds)
    ]


def wait_for_events(
    read_entries: Callable[[], Sequence[Entry]],
    sleep: Callable[[float], None],
    now: Callable[[], float],
    poll_seconds: float,
    kinds: Collection[str],
    nap: Callable[[float], bool] | None = None,
) -> Callable[[float], bool]:
    """A wait(interval) that polls in `poll_seconds` steps: True on a new event of `kinds`, False when the interval elapses.

    Every id it returned for is remembered, so one entry wakes the chair once, even if a later fetch fails. With `nap`,
    each step waits through it instead of `sleep`, and a nap that returns True (a store notification it watches for)
    ends the wait at once, True.
    """
    returned: set[str] = set()  # the one piece of state; a wake is spent per id for the life of the wait

    def poll() -> list[Entry]:
        try:
            return new_wake_events(read_entries(), returned, kinds)
        except Exception:  # a failed fetch is a quiet poll, not a crash of the chair loop
            return []

    def wait(interval: float) -> bool:
        deadline = now() + interval
        while True:
            fresh = poll()
            if fresh:
                returned.update(i for i in map(_entry_id, fresh) if i is not None)
                return True
            remaining = deadline - now()
            if remaining <= 0:
                return False
            if nap is None:
                sleep(min(poll_seconds, remaining))
            elif nap(min(poll_seconds, remaining)):
                return True

    return wait


def wait_for_run_exit(
    read_entries: Callable[[], Sequence[Entry]],
    sleep: Callable[[float], None],
    now: Callable[[], float],
    poll_seconds: float,
    nap: Callable[[float], bool] | None = None,
) -> Callable[[float], bool]:
    """A wait(interval) that returns True on a new run_exited id, or a `nap` that saw a store write, and False when
    the interval elapses."""
    return wait_for_events(read_entries, sleep, now, poll_seconds, (RUN_EXITED,), nap)
