from __future__ import annotations

from collections.abc import Mapping
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


def land_gate_level(land_at: str) -> str:
    """The granularity at which the human approval gate applies: the `land_at` value itself."""
    return _checked(land_at, "gate")
