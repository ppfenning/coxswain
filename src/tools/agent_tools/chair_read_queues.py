"""Edge. The two facts that say who plans a kind of the chair's work: the profile's `chair.queues`, and the kinds whose
worker lease is live (`chair_kinds`). Both the tick and every queue worker read them, each pass, the same way.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from agent_tools import chair_kinds, route, run_store
from core.jsontypes import is_mapping

__all__ = ["queues_keeping_last_good", "read_chair_queues", "read_queue_workers"]


def read_chair_queues(profile_path: Path) -> frozenset[str] | route.ProfileError:
    """The kinds the profile's `chair.queues` lists. A missing profile lists none; one that will not parse comes back
    as the `ProfileError`, so the caller can keep its last good read."""
    try:
        text = profile_path.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    try:
        chair: object = route.parse_profile(text).get("chair")
    except route.ProfileError as exc:
        return exc
    return chair_kinds.queued_kinds(chair.get("queues") if is_mapping(chair) else None)


def queues_keeping_last_good(profile_path: Path) -> Callable[[], frozenset[str]]:
    """A reader of `chair.queues` that re-reads the profile on every call. A read that fails keeps the last good value,
    and the first read that fails moves nothing (the default, none): an edit half saved never flips a kind."""
    good: list[frozenset[str]] = [frozenset()]  # edge state: the last good read

    def read() -> frozenset[str]:
        value = read_chair_queues(profile_path)
        if not isinstance(value, route.ProfileError):
            good[0] = value
        return good[0]

    return read


def read_queue_workers(runs_dir: Path, now: str) -> frozenset[str]:
    """The kinds whose worker lease is unexpired at `now`, from one read of the leases table (shared with every other
    lease check in the same two-second window). An unreadable store names none."""
    try:
        table = run_store.lease_table_for(str(runs_dir), int(time.monotonic() // run_store.LEASE_SNAPSHOT_S))
    except Exception:  # the tick then plans the kind itself, as it always has
        return frozenset()
    return chair_kinds.live_kinds(table, now)
