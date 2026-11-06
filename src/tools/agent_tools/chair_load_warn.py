"""The chair's overloaded-host warning: one echoed line per host on its transition into overload."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from agent_tools.host_load import overload_warnings

__all__ = ["warn_overloaded"]


def warn_overloaded(
    rows: Iterable[Mapping[str, object]], echo: Callable[[str], None], warned: frozenset[str],
) -> frozenset[str]:
    """Echoes a line for each overloaded host not in `warned`; returns the hosts overloaded now, so a recovered host clears."""
    current = overload_warnings(rows)
    for name in sorted(current.keys() - warned):
        echo(f"chair: host {name} overloaded: {current[name]}")
    return frozenset(current)
