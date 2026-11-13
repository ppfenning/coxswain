"""The chair's quarantine marks on a task's store row: one spelling, read by the chair and the epic alike."""

from __future__ import annotations

import pytest

from core import workstore


def test_the_marks_are_the_extra_keys_both_sides_read() -> None:
    assert (workstore.TRIAGE_FLAG, workstore.HOLD_REASON, workstore.TRIAGE_RECORDS) == ("triage_requested", "hold_reason", "triage")
    assert workstore.HOLD_MARK == "needs you:"


def test_a_hold_text_starts_with_the_mark_and_is_one_line() -> None:
    assert workstore.hold_text("triage says\n  a person decides") == "needs you: triage says a person decides"
    assert workstore.hold_text("needs you: already marked") == "needs you: already marked"


@pytest.mark.parametrize(
    ("state", "extra", "held"),
    [
        ("blocked", {"hold_reason": "needs you: pick one"}, True),
        ("blocked", {"hold_reason": "held by someone on a day: other"}, False),
        ("blocked", {}, False),
        ("ready", {"hold_reason": "needs you: pick one"}, False),
        ("blocked", None, False),
    ],
)
def test_a_task_is_held_only_when_blocked_with_the_mark(state, extra, held) -> None:
    assert workstore.is_held({"state": state, "extra": extra}) is held
