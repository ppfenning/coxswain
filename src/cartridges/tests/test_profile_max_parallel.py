from __future__ import annotations

from typing import Any

from core.catalog import parse_catalog
from core.profile import Profile, resolve_profile

_ROW: dict[str, Any] = {
    "id": "claude-sonnet-5-5",
    "aliases": [],
    "provider": "anthropic",
    "price": {"input": 1, "output": 2, "cache_write": 1, "cache_read": 1},
    "effort": ["low"],
    "classes": ["reason"],
    "context_window": None,
}
_CATALOG = parse_catalog({"models": [_ROW]})
_TIERS = {"standard": "claude-sonnet-5-5"}


def _resolve(**extra: Any) -> Profile | list[str]:
    return resolve_profile({"tiers": _TIERS, **extra}, _CATALOG)  # type: ignore[arg-type]


def test_a_positive_integer_is_exposed() -> None:
    profile = _resolve(max_parallel=2)
    assert isinstance(profile, Profile)
    assert profile.max_parallel == 2


def test_an_absent_value_is_none() -> None:
    profile = _resolve()
    assert isinstance(profile, Profile)
    assert profile.max_parallel is None


def test_zero_is_refused_with_a_message() -> None:
    assert _resolve(max_parallel=0) == ["max_parallel must be a positive integer, got 0"]


def test_a_bad_tier_and_a_bad_max_parallel_are_both_reported() -> None:
    result = resolve_profile({"tiers": {"deep": "opus"}, "max_parallel": 0}, _CATALOG)
    assert result == [
        "tier 'deep': alias 'opus': no catalog row for family 'opus'",
        "max_parallel must be a positive integer, got 0",
    ]


def test_a_non_integer_is_refused_with_a_message() -> None:
    assert _resolve(max_parallel="two") == ["max_parallel must be a positive integer, got 'two'"]
    assert _resolve(max_parallel=2.5) == ["max_parallel must be a positive integer, got 2.5"]
    assert _resolve(max_parallel=True) == ["max_parallel must be a positive integer, got True"]
