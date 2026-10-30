"""Pure lease standing and run status; timestamps are YYYY-MM-DDTHH:MM:SSZ strings, compared as text."""

from __future__ import annotations

NONE = "none"
HELD = "held"
EXPIRED = "expired"
RELEASED = "released"
FENCED = "fenced"

EXITED = "exited"
RUNNING = "running"


def standing(lease: tuple[str, str, str] | None, run: str, now: str) -> str:
    """Classify a (holder, expires_at, heartbeat_at) lease row for `run` at `now`."""
    if lease is None:
        return NONE
    holder, expires_at, heartbeat_at = lease
    if holder != run:
        return FENCED
    if expires_at > now:
        return HELD
    if expires_at > heartbeat_at:
        return EXPIRED
    return RELEASED


def status(process_alive: bool, standing: str) -> str:
    """Name a run's status; a live run with a lapsed or lost lease is fenced because the epoch fence refuses its writes."""
    if not process_alive:
        return EXITED
    if standing in (FENCED, EXPIRED):
        return FENCED
    return RUNNING
