"""Chair wake: end the wait between ticks early when a run publishes `run_exited` on the bus."""

from __future__ import annotations

from collections.abc import Callable, Collection

from agent_tools.courier import parse_run_exited

__all__ = ["new_run_exits", "wait_for_run_exit"]


def new_run_exits(entries: list[dict], seen_ids: Collection[str]) -> list[dict]:
    """Entries `parse_run_exited` accepts that are unacked and whose id is not in `seen_ids`."""
    return [
        e for e in entries
        if parse_run_exited(e) is not None and not e.get("ack") and isinstance(e.get("id"), str) and e["id"] not in seen_ids
    ]


def wait_for_run_exit(
    read_entries: Callable[[], list[dict]],
    sleep: Callable[[float], None],
    now: Callable[[], float],
    poll_seconds: float,
) -> Callable[[float], bool]:
    """A wait(interval) that polls in `poll_seconds` steps: True on a new run_exited id, False when the interval elapses.

    Every id it returned for is remembered, so one entry wakes the chair once, even if a later fetch fails.
    """
    returned: set[str] = set()  # the one piece of state; a wake is spent per id for the life of the wait

    def poll() -> list[dict]:
        try:
            return new_run_exits(read_entries(), returned)
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
