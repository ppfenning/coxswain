"""The chair's bounds: the ceilings a routing decision may not exceed.

`from_policy` is pure. It takes the `policy:` mapping and the launch flags as
plain values and returns a `ChairBounds` or a list of error strings. Per field
a launch flag wins, then the cartridge policy value, then a named default. Each
field that fell back to its default is named in `ChairBounds.defaulted`.
`load_bounds` is the only edge: it reads the cartridge yaml and hands the
`policy` mapping over.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from core.policy import EFFORT_LADDER, TIER_LADDER

__all__ = [
    "DEFAULT_CLASS_CEILING",
    "DEFAULT_EFFORT_CEILING",
    "DEFAULT_PACING_STATE",
    "PACING_STATES",
    "ChairBounds",
    "from_policy",
    "load_bounds",
]

# The verdicts agent_tools/pacing.py assess() returns in coxswain-tools.
PACING_STATES = ("go", "go_degraded", "stop")

DEFAULT_CLASS_CEILING = TIER_LADDER[0]
DEFAULT_EFFORT_CEILING = EFFORT_LADDER[0]
DEFAULT_PACING_STATE = PACING_STATES[0]


@dataclass(frozen=True)
class ChairBounds:
    class_ceiling: str
    effort_ceiling: str
    node_budget_cap_usd: float | None  # None means no cap
    run_budget_cap_usd: float | None  # None means no cap
    pacing_state: str
    defaulted: tuple[str, ...]  # fields that used their default, neither flag nor policy


def _pick(flags: Mapping[str, Any], flag_key: str, policy_value: Any, default: Any) -> tuple[Any, bool]:
    """Flag, then policy value, then default. The bool is True when the default won."""
    if flags.get(flag_key) is not None:
        return flags[flag_key], False
    if policy_value is not None:
        return policy_value, False
    return default, True


def _name_error(field: str, value: Any, allowed: tuple[str, ...]) -> list[str]:
    return [] if value in allowed else [f"{field}: unknown value {value!r}, expected one of {list(allowed)}"]


def _cap_error(field: str, value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, bool) or not isinstance(value, int | float):
        return [f"{field}: must be a number or null, got {value!r}"]
    return [f"{field}: must not be negative, got {value!r}"] if value < 0 else []


def from_policy(policy: Mapping[str, Any], flags: Mapping[str, Any]) -> ChairBounds | list[str]:
    """Build the bounds from `policy:` and launch flags, or return every error found.

    Flag keys: tier_ceiling, effort_ceiling, node_budget_cap_usd, run_budget_cap_usd,
    pacing_state. Policy keys sit under `pacing`: the head of `tier_ladder` is the
    class ceiling, and `effort_ceiling`, `node_budget_cap_usd`, `run_budget_cap_usd`
    and `pacing_state` are read as named. A `None` value counts as missing.
    """
    pacing = policy.get("pacing") or {}
    tier_ladder = tuple(pacing.get("tier_ladder") or TIER_LADDER)
    effort_ladder = tuple(pacing.get("effort_ladder") or EFFORT_LADDER)
    ladder_head = pacing["tier_ladder"][0] if pacing.get("tier_ladder") else None
    picks = {
        "class_ceiling": _pick(flags, "tier_ceiling", ladder_head, DEFAULT_CLASS_CEILING),
        "effort_ceiling": _pick(flags, "effort_ceiling", pacing.get("effort_ceiling"), DEFAULT_EFFORT_CEILING),
        "node_budget_cap_usd": _pick(flags, "node_budget_cap_usd", pacing.get("node_budget_cap_usd"), None),
        "run_budget_cap_usd": _pick(flags, "run_budget_cap_usd", pacing.get("run_budget_cap_usd"), None),
        "pacing_state": _pick(flags, "pacing_state", pacing.get("pacing_state"), DEFAULT_PACING_STATE),
    }
    values = {field: value for field, (value, _) in picks.items()}
    errors = [
        *_name_error("class_ceiling", values["class_ceiling"], tier_ladder),
        *_name_error("effort_ceiling", values["effort_ceiling"], effort_ladder),
        *_name_error("pacing_state", values["pacing_state"], PACING_STATES),
        *_cap_error("node_budget_cap_usd", values["node_budget_cap_usd"]),
        *_cap_error("run_budget_cap_usd", values["run_budget_cap_usd"]),
    ]
    if errors:
        return errors
    defaulted = tuple(field for field, (_, used_default) in picks.items() if used_default)
    return ChairBounds(**values, defaulted=defaulted)


def load_bounds(cartridge_path: Path, flags: Mapping[str, Any]) -> ChairBounds | list[str]:
    """The edge: read the cartridge yaml and hand its `policy` mapping to `from_policy`."""
    document = yaml.safe_load(Path(cartridge_path).read_text()) or {}
    return from_policy(document.get("policy") or {}, flags)
