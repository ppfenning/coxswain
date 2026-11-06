"""The rule that splits a host's cores among the lanes of a local epic launch.

Pure: the cpu count and the environment are parameters, so nothing here reads the machine.
"""

from __future__ import annotations

from collections.abc import Mapping

LANE_CAPACITY_VAR = "COX_LANE_CAPACITY"  # set by the tools side on a local epic launch
XDIST_VAR = "PYTEST_XDIST_AUTO_NUM_WORKERS"


def lane_workers(cores: int, capacity: int) -> int:
    """Floor of cores // capacity, never below 1; a cores or capacity below 1 counts as 1."""
    return max(1, max(cores, 1) // max(capacity, 1))


def _positive_int(text: str | None) -> int | None:
    try:
        value = int((text or "").strip())
    except ValueError:
        return None
    return value if value >= 1 else None


def lane_workers_from(environ: Mapping[str, str], cores: int) -> int | None:
    """None when the lane states no valid capacity, so a lane without one changes nothing."""
    capacity = _positive_int(environ.get(LANE_CAPACITY_VAR))
    return None if capacity is None else lane_workers(cores, capacity)


def workers_env(environ: Mapping[str, str], workers: int) -> dict[str, str]:
    """An operator's explicit XDIST_VAR wins, so an already-set one yields no override."""
    return {} if XDIST_VAR in environ else {XDIST_VAR: str(workers)}


def check_env(environ: Mapping[str, str], cores: int, capacity: int | None) -> dict[str, str] | None:
    """The full child environment for a check spawn; None without a capacity of 1 or more, so the caller changes nothing."""
    if capacity is None or capacity < 1:
        return None
    return {**environ, **workers_env(environ, lane_workers(cores, capacity))}
