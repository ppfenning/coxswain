from datetime import UTC, datetime, timedelta

from agent_tools.chair_lease_refusal import (
    LEASE_TTL_S,
    STATUS,
    LeaseRefusal,
    parse_lease_refusal,
    stuck_due,
)

HARNESS = "run: runS-1 holds runs:runS; refusing to start runS"
FIRST = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
EXPECTED = LeaseRefusal(holder_run="runS-1", lease="runs:runS", refused_run="runS")


def test_status_is_lease_held():
    assert STATUS == "lease_held"


def test_the_harness_line_parses_to_the_three_fields():
    assert parse_lease_refusal(HARNESS + "\n") == EXPECTED


def test_the_routing_prefixed_line_parses_to_the_same_fields():
    assert parse_lease_refusal("routing: launch failed: " + HARNESS) == EXPECTED


def test_unrelated_output_is_none():
    assert parse_lease_refusal("routing: launch failed: boom") is None


def test_empty_output_is_none():
    assert parse_lease_refusal("") is None


def test_a_land_refusal_line_is_none():
    assert parse_lease_refusal("chair run: refusing to start, no lease taken; not wired: record") is None


def test_a_live_holder_under_the_ttl_is_not_stuck():
    assert stuck_due(FIRST, FIRST + timedelta(seconds=LEASE_TTL_S - 1), True) is False


def test_a_live_holder_at_exactly_the_ttl_is_stuck():
    assert stuck_due(FIRST, FIRST + timedelta(seconds=LEASE_TTL_S), True) is True


def test_a_dead_holder_past_the_ttl_is_not_stuck():
    assert stuck_due(FIRST, FIRST + timedelta(seconds=LEASE_TTL_S + 60), False) is False
