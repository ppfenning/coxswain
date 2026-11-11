"""Chair wake: end the wait between ticks early when a run exits, intake arrives or an operator directive lands on the bus."""

from __future__ import annotations

from collections.abc import Callable, Collection

from agent_tools.courier import parse_run_exited

__all__ = ["new_run_exits", "new_wake_events", "wait_for_events", "wait_for_run_exit"]

RUN_EXITED = "run_exited"
INTAKE = "intake"  # courier has no intake event: a plain entry, with no `event`, whose ref is `coxswain://intake/<id>`
CHAT_DIRECTIVE = "chat_directive"  # a courier event name, matched on the entry's `event`


def _wakes(entry: dict, kind: str) -> bool:
    if kind == RUN_EXITED:
        return parse_run_exited(entry) is not None
    if kind == INTAKE:  # every entry carries a `ref`, so an event on an intake ref (an answer, an applied note) is not new intake
        return entry.get("event") is None and str(entry.get("ref", "")).startswith("coxswain://intake/")
    return entry.get("event") == kind


def new_wake_events(entries: list[dict], seen_ids: Collection[str], kinds: Collection[str]) -> list[dict]:
    """Entries of one of `kinds` that are unacked and whose id is not in `seen_ids`."""
    return [
        e for e in entries
        if not e.get("ack") and isinstance(e.get("id"), str) and e["id"] not in seen_ids and any(_wakes(e, k) for k in kinds)
    ]


def new_run_exits(entries: list[dict], seen_ids: Collection[str]) -> list[dict]:
    """Entries `parse_run_exited` accepts that are unacked and whose id is not in `seen_ids`."""
    return new_wake_events(entries, seen_ids, (RUN_EXITED,))


def wait_for_events(
    read_entries: Callable[[], list[dict]],
    sleep: Callable[[float], None],
    now: Callable[[], float],
    poll_seconds: float,
    kinds: Collection[str],
) -> Callable[[float], bool]:
    """A wait(interval) that polls in `poll_seconds` steps: True on a new event of `kinds`, False when the interval elapses.

    Every id it returned for is remembered, so one entry wakes the chair once, even if a later fetch fails.
    """
    returned: set[str] = set()  # the one piece of state; a wake is spent per id for the life of the wait

    def poll() -> list[dict]:
        try:
            return new_wake_events(read_entries(), returned, kinds)
        except Exception:  # a failed fetch is a quiet poll, not a crash of the chair loop
            return []

    def wait(interval: float) -> bool:
        deadline = now() + interval
        while True:
            fresh = poll()
            if fresh:
                returned.update(e["id"] for e in fresh)
                return True
            remaining = deadline - now()
            if remaining <= 0:
                return False
            sleep(min(poll_seconds, remaining))

    return wait


def wait_for_run_exit(
    read_entries: Callable[[], list[dict]],
    sleep: Callable[[float], None],
    now: Callable[[], float],
    poll_seconds: float,
) -> Callable[[float], bool]:
    """A wait(interval) that returns True on a new run_exited id and False when the interval elapses."""
    return wait_for_events(read_entries, sleep, now, poll_seconds, (RUN_EXITED,))
