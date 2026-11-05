"""Shadow tier core: parse the shadow_tier setting and compare two verdicts. Pure; no I/O, no clock."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from runner.tier_resolution import to_class

# Only roles that judge, never roles that write code.
VERDICT_KEYS = {"handoff": "complete", "validate_chunk": "satisfied"}
SHADOW_ROLES = tuple(VERDICT_KEYS)


@dataclass(frozen=True)
class ShadowConfig:
    """Role to shadow tier pairs, sorted by role."""

    tiers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ConfigError:
    message: str
    role: str | None = None


@dataclass(frozen=True)
class Comparison:
    """None in both fields means a verdict was unreadable and the pair is not counted."""

    agreed: bool | None
    costly: bool | None


def _check_pair(role: object, tier: object) -> ConfigError | None:
    if not isinstance(role, str) or role not in SHADOW_ROLES:
        return ConfigError(f"shadow_tier role {role!r} is not one of {', '.join(SHADOW_ROLES)}", str(role))
    if not isinstance(tier, str) or not tier:
        return ConfigError(f"shadow_tier tier for {role} must be a non-empty string, got {tier!r}", role)
    if to_class(tier) is None:
        return ConfigError(f"shadow_tier tier {tier!r} for {role} is not a known tier or class", role)
    return None


def parse_shadow_tier(raw: object) -> ShadowConfig | ConfigError:
    """Absent or empty parses to an empty config; the first bad entry in role order is the error."""
    if raw is None:
        return ShadowConfig()
    if not isinstance(raw, Mapping):
        return ConfigError(f"shadow_tier must be a mapping of role to tier, got {type(raw).__name__}")
    pairs = sorted(raw.items(), key=lambda kv: str(kv[0]))
    errors = [e for e in (_check_pair(role, tier) for role, tier in pairs) if e is not None]
    return errors[0] if errors else ShadowConfig(tuple(pairs))


def shadow_verdict(role: str, result: Mapping[str, object]) -> bool | None:
    """The role's boolean verdict from a node result; None when the key is missing or not a bool."""
    key = VERDICT_KEYS.get(role)
    value = result.get(key) if key is not None else None
    return value if isinstance(value, bool) else None


def compare(role: str, acting: Mapping[str, object], shadow: Mapping[str, object]) -> Comparison:
    """costly is the shadow tier passing what the acting tier stopped."""
    a = shadow_verdict(role, acting)
    s = shadow_verdict(role, shadow)
    if a is None or s is None:
        return Comparison(None, None)
    return Comparison(agreed=a == s, costly=s and not a)
