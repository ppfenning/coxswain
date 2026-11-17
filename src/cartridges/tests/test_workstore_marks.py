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


def test_the_fresh_start_mark_is_an_extra_key_both_sides_read() -> None:
    assert workstore.FRESH_AFTER_RUN == "fresh_after_run"


@pytest.mark.parametrize(("run", "number"), [("init-a-9", 9), ("init-a-10", 10), ("init-a", -1), ("", -1), (None, -1)])
def test_a_run_number_is_the_tail_of_its_id(run, number) -> None:
    assert workstore.run_number(run) == number


def test_the_newest_run_is_the_highest_number_of_that_initiative_only() -> None:
    runs = {"init-a-9", "init-a-10", "init-a-2", "init-ab-40", "other-77"}
    assert workstore.newest_run("init-a", runs) == "init-a-10"
    assert workstore.newest_run("init-c", runs) is None


def test_the_cutoff_is_the_later_of_the_task_and_initiative_marks() -> None:
    task = {workstore.FRESH_AFTER_RUN: "init-a-9"}
    initiative = {workstore.FRESH_AFTER_RUN: "init-a-7"}
    assert workstore.fresh_cutoff(task, initiative) == 9
    assert workstore.fresh_cutoff(None, initiative) == 7
    assert workstore.fresh_cutoff({}, None) == -1


def test_only_a_run_after_the_cutoff_counts() -> None:
    assert not workstore.counts_after("init-a-9", 9)
    assert workstore.counts_after("init-a-10", 9)
    assert not workstore.counts_after("", 9)
    assert workstore.counts_after("", -1)


def test_a_triage_flag_names_its_run() -> None:
    assert workstore.triage_flag_run("triage:init-a-unit:init-a-9") == "init-a-9"
    assert workstore.triage_flag_run("triage") == ""
