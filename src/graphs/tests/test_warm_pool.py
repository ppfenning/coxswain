"""Warm pool rules: one literal call per rule."""

from __future__ import annotations

from harness.warm_pool import (
    MISSING_LOCK_DIGEST,
    PoolEntry,
    choose_evictions,
    lock_digest,
    pool_size,
    setup_record,
    sync_decision,
)


def _entry(slot: str, last_used: float, leased: bool = False) -> PoolEntry:
    return PoolEntry(path=f"/pool/{slot}", slot=slot, synced_digest="d", last_used=last_used, leased=leased)


def test_lock_digest_is_sha256_hex() -> None:
    assert lock_digest(b"a") == "ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb"


def test_lock_digest_missing_lockfile_is_sentinel() -> None:
    assert lock_digest(None) == MISSING_LOCK_DIGEST


def test_empty_lockfile_is_not_the_sentinel() -> None:
    assert lock_digest(b"") != MISSING_LOCK_DIGEST


def test_unchanged_digest_is_warm() -> None:
    assert sync_decision("abc", "abc") == "warm"


def test_changed_digest_resyncs() -> None:
    assert sync_decision("abc", "def") == "resync"


def test_never_synced_resyncs() -> None:
    assert sync_decision(None, "abc") == "resync"


def test_pool_size_adds_spare() -> None:
    assert pool_size(4) == 5


def test_pool_size_floors_at_one() -> None:
    assert pool_size(0, spare=0) == 1


def test_leased_entry_is_never_evicted() -> None:
    assert choose_evictions([_entry("a", 1.0, leased=True), _entry("b", 2.0)], keep=0) == ["b"]


def test_evictions_oldest_first() -> None:
    assert choose_evictions([_entry("a", 3.0), _entry("b", 1.0), _entry("c", 2.0)], keep=0) == ["b", "c", "a"]


def test_evictions_choose_exactly_the_excess() -> None:
    assert choose_evictions([_entry("a", 3.0), _entry("b", 1.0), _entry("c", 2.0)], keep=2) == ["b"]


def test_evictions_none_when_within_keep() -> None:
    assert choose_evictions([_entry("a", 3.0), _entry("b", 1.0)], keep=2) == []


def test_evictions_tie_on_last_used_breaks_by_slot() -> None:
    assert choose_evictions([_entry("b", 1.0), _entry("a", 1.0)], keep=1) == ["a"]


def test_evictions_negative_keep_counts_as_zero() -> None:
    assert choose_evictions([_entry("a", 1.0)], keep=-5) == ["a"]


def test_setup_record_rounds_to_milliseconds() -> None:
    assert setup_record("warm", 1.23456) == {"mode": "warm", "seconds": 1.235}


def test_setup_record_cold_mode() -> None:
    assert setup_record("cold", 2.0) == {"mode": "cold", "seconds": 2.0}


def test_setup_record_rejects_other_mode_with_error_value() -> None:
    assert setup_record("tepid", 1.0) == {"error": "unknown setup mode: 'tepid'"}
