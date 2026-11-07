"""Process-wide flag that says this run is being terminated."""

from __future__ import annotations

import threading

_TERMINATING = threading.Event()


def mark_terminating() -> None:
    """Set the flag; only the SIGTERM exit calls this, and nothing clears it inside a process."""
    _TERMINATING.set()


def is_terminating() -> bool:
    """Read the flag, which only the SIGTERM exit sets and nothing clears inside a process."""
    return _TERMINATING.is_set()
