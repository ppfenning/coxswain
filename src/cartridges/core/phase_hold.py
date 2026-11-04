from __future__ import annotations


def phase_holds(awaiting_land: int, buildable: int) -> bool:
    """A phase holds only when something awaits land and nothing is buildable; one approved-unlanded task beside one ready task with met needs does not hold."""
    return awaiting_land > 0 and buildable == 0
