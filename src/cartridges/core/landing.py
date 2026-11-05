from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

LAND_AT_VALUES = ("ticket", "level", "task")
DEFAULT_LAND_AT = "ticket"


def _checked(value: Any, where: str) -> str:
    if isinstance(value, str) and value in LAND_AT_VALUES:
        return value
    allowed = ", ".join(repr(v) for v in LAND_AT_VALUES)
    raise ValueError(f"{where} land_at must be one of {allowed}, got {value!r}")


def resolve_land_at(cartridge: Mapping[str, Any], initiative_land_at: str | None) -> str:
    """The initiative value, else the cartridge or overlay value, else `ticket`; every value given is validated."""
    landing = cartridge.get("landing_areas")
    configured = landing.get("land_at") if isinstance(landing, Mapping) else None
    # Both places are checked even when the initiative overrides, so a bad cartridge never hides.
    checked_config = None if configured is None else _checked(configured, "cartridge")
    checked_initiative = None if initiative_land_at is None else _checked(initiative_land_at, "initiative")
    return checked_initiative or checked_config or DEFAULT_LAND_AT


_BRANCH_FORBIDDEN = frozenset("~^:?*[\\")


def valid_branch(value: Any, where: str) -> str:
    """A bare branch name under git's ref-name rules; a non-string or any broken rule raises."""
    ok = (
        isinstance(value, str)
        and value != ""
        and not any(c.isspace() or c < " " or c == "\x7f" or c in _BRANCH_FORBIDDEN for c in value)
        and ".." not in value
        and "@{" not in value
        and not value.startswith(("-", "/"))
        and not value.endswith(("/", ".", ".lock"))
    )
    if ok:
        return value
    raise ValueError(f"{where} target_branch must be a valid branch name, got {value!r}")


def resolve_target_branch(
    cartridge: Mapping[str, Any], epic_branch: str | None, initiative_branch: str | None
) -> str | None:
    """The initiative value, else the epic, else `landing_areas.target_branch`, else None; every value given is validated."""
    landing = cartridge.get("landing_areas")
    configured = landing.get("target_branch") if isinstance(landing, Mapping) else None
    # Validate all three even when a higher one wins, so a bad lower value never hides.
    checked = [
        None if value is None else valid_branch(value, where)
        for value, where in ((initiative_branch, "initiative"), (epic_branch, "epic"), (configured, "cartridge"))
    ]
    return next((value for value in checked if value is not None), None)


def land_gate_level(land_at: str) -> str:
    """The granularity at which the human approval gate applies: the `land_at` value itself."""
    return _checked(land_at, "gate")


def phase_land_deferred(land_at: str, phase: str, phases: Sequence[str]) -> bool:
    """True only at `ticket` for a phase that is not the last of `phases` (workstore.phases order)."""
    return _checked(land_at, "phase") == "ticket" and bool(phases) and phase != phases[-1]


def phase_counts_complete(land_at: str, states: Sequence[tuple[str, bool]]) -> bool:
    """Every (state, merged) is done or dropped, or approved and merged unless `land_at` is `task`; empty is True."""
    merged_counts = _checked(land_at, "phase") != "task"
    return all(s in ("done", "dropped") or (merged_counts and s == "approved" and merged) for s, merged in states)


def need_met(land_at: str, state: str, merged: bool) -> bool:
    """An unmerged approved task is met only above `land_at` task."""
    level = _checked(land_at, "need")
    return state in ("done", "dropped") or (state == "approved" and (merged or level != "task"))
