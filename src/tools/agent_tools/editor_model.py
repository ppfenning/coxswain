"""Path reads over a resolved cartridge, shared by the settings models.

Pure: plain dicts in, plain data out. What is left of the old cartridge editor: its rows, state machine and frame
went with the curses screens; the settings models (settings_builds, settings_hosts, settings_model, settings_plan)
still read the resolved cartridge through these helpers.
"""

from __future__ import annotations

import functools
from typing import Any

_MISSING = object()


def _step(node, part):
    if isinstance(node, dict) and part in node:
        return node[part]
    if part == "crew" and isinstance(node, dict) and "cast" in node:
        return node["cast"]
    return _MISSING


def _get(value: dict, path: str) -> Any:
    """The value at dotted `path`, `_MISSING` when any part is absent; `crew` reads a cartridge's older `cast` key."""
    return functools.reduce(_step, path.split("."), value)


def _crew(resolved: dict) -> dict:
    if not isinstance(resolved, dict):
        return {}
    return resolved["crew"] or {} if "crew" in resolved else (resolved.get("cast") or {})


def _seat_field_paths(resolved: dict, seat: str, value) -> list[str]:
    if not isinstance(value, dict):
        return [f"crew.{seat}"]
    return [
        f"crew.{seat}.{field}"
        for field in ("enabled", "skills")
        if _get(resolved, f"crew.{seat}.{field}") is not _MISSING
    ]
