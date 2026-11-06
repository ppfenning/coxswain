from __future__ import annotations

from typing import Any

from core.catalog import parse_catalog
from core.profile import MergeQueue, Profile, parse_merge_queue, resolve_profile

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


def test_an_absent_key_is_none() -> None:
    profile = _resolve()
    assert isinstance(profile, Profile)
    assert profile.merge_queue is None
    assert parse_merge_queue(None) is None


def test_a_valid_mapping_gives_the_dataclass() -> None:
    profile = _resolve(merge_queue={"batch_size": 4, "wait_s": 2.5})
    assert isinstance(profile, Profile)
    assert profile.merge_queue == MergeQueue(batch_size=4, wait_s=2.5)


def test_missing_inner_keys_take_the_defaults() -> None:
    assert parse_merge_queue({}) == MergeQueue(1, 0.0)
    assert parse_merge_queue({"batch_size": 3}) == MergeQueue(3, 0.0)
    assert parse_merge_queue({"wait_s": 5}) == MergeQueue(1, 5.0)


def test_a_bool_is_refused_for_both_keys() -> None:
    assert _resolve(merge_queue={"batch_size": True}) == [
        "merge_queue.batch_size must be an integer of at least 1, got True"
    ]
    assert _resolve(merge_queue={"wait_s": True}) == ["merge_queue.wait_s must be a number of at least 0, got True"]


def test_a_zero_batch_size_is_refused() -> None:
    assert _resolve(merge_queue={"batch_size": 0}) == ["merge_queue.batch_size must be an integer of at least 1, got 0"]


def test_a_negative_wait_is_refused() -> None:
    assert _resolve(merge_queue={"wait_s": -1}) == ["merge_queue.wait_s must be a number of at least 0, got -1"]


def test_a_non_mapping_is_refused() -> None:
    assert _resolve(merge_queue="fast") == ["merge_queue must be a mapping, got 'fast'"]
    assert _resolve(merge_queue=[1]) == ["merge_queue must be a mapping, got [1]"]


def test_an_unknown_key_is_refused_by_name() -> None:
    assert _resolve(merge_queue={"bogus": 1}) == ["merge_queue has unknown key(s) 'bogus'"]


def test_a_bad_tier_and_a_bad_merge_queue_are_both_reported() -> None:
    result = resolve_profile({"tiers": {"deep": "opus"}, "merge_queue": {"batch_size": 0}}, _CATALOG)
    assert result == [
        "tier 'deep': alias 'opus': no catalog row for family 'opus'",
        "merge_queue.batch_size must be an integer of at least 1, got 0",
    ]


def test_an_otherwise_valid_profile_loads_with_merge_queue_set() -> None:
    profile = _resolve(max_parallel=2, merge_queue={"batch_size": 2})
    assert isinstance(profile, Profile)
    assert profile.max_parallel == 2
    assert profile.merge_queue == MergeQueue(batch_size=2, wait_s=0.0)
