from agent_tools.chair_decompose_miss import (
    MISS_BACKOFF_CAP_SECONDS,
    DecomposeMiss,
    backoff_seconds,
    escalation_reason,
    is_skipped,
    needs_escalation,
    next_miss,
)

T0 = "2026-11-11T10:00:00Z"


def _miss(count: int, fingerprint: str = "fp1") -> DecomposeMiss:
    return DecomposeMiss("intake-1", "ideas/a.md", count, T0, fingerprint)


def test_first_miss_starts_at_one_and_repeat_adds_one() -> None:
    first = next_miss(None, "intake-1", "ideas/a.md", "fp1", T0)
    assert first == DecomposeMiss("intake-1", "ideas/a.md", 1, T0, "fp1")
    assert next_miss(first, "intake-1", "ideas/a.md", "fp1", "2026-11-11T10:05:00Z").count == 2


def test_changed_fingerprint_resets_to_one() -> None:
    assert next_miss(_miss(2), "intake-1", "ideas/a.md", "fp2", T0).count == 1


def test_backoff_doubles_per_miss() -> None:
    assert [backoff_seconds(c, 60) for c in (1, 2, 3)] == [240, 480, 960]


def test_backoff_caps_at_two_hours() -> None:
    assert backoff_seconds(10, 60) == MISS_BACKOFF_CAP_SECONDS == 7200


def test_skipped_inside_window() -> None:
    assert is_skipped(_miss(1), "fp1", "2026-11-11T10:03:59Z", 60)


def test_not_skipped_after_window() -> None:
    assert not is_skipped(_miss(1), "fp1", "2026-11-11T10:04:00Z", 60)


def test_escalated_miss_stays_skipped_past_cap() -> None:
    assert is_skipped(_miss(3), "fp1", "2026-11-11T13:00:00Z", 60)


def test_changed_fingerprint_is_never_skipped() -> None:
    assert not is_skipped(_miss(3), "fp2", T0, 60)


def test_escalation_fires_at_three_unreported() -> None:
    assert needs_escalation(_miss(3), False)


def test_escalation_not_repeated_once_reported() -> None:
    assert not needs_escalation(_miss(3), True)


def test_no_escalation_at_two() -> None:
    assert not needs_escalation(_miss(2), False)


def test_escalation_reason_names_intake_path_and_count() -> None:
    reason = escalation_reason(_miss(3))
    assert "intake-1" in reason and "ideas/a.md" in reason and "3" in reason
