from datetime import UTC, datetime, timedelta

from agent_tools.chair import DEFAULT_LEASE_TTL_SECONDS, lease_ttl_for

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def test_an_until_four_hours_ahead_covers_four_hours_plus_the_margin():
    record = {"until": (NOW + timedelta(hours=4)).isoformat()}
    assert lease_ttl_for(record, NOW) == 4 * 3600 + DEFAULT_LEASE_TTL_SECONDS


def test_no_until_gives_the_default():
    assert lease_ttl_for({}, NOW) == DEFAULT_LEASE_TTL_SECONDS


def test_a_passed_until_gives_the_default():
    assert lease_ttl_for({"until": (NOW - timedelta(hours=1)).isoformat()}, NOW) == DEFAULT_LEASE_TTL_SECONDS


def test_a_naive_until_gives_the_default():
    assert lease_ttl_for({"until": "2026-09-26T16:00:00"}, NOW) == DEFAULT_LEASE_TTL_SECONDS


def test_a_short_until_still_gives_at_least_the_default():
    assert lease_ttl_for({"until": (NOW + timedelta(seconds=30)).isoformat()}, NOW, margin_seconds=0) == DEFAULT_LEASE_TTL_SECONDS
