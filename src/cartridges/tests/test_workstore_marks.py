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


@pytest.mark.parametrize(
    ("run", "series"),
    [("init-a-9", ("init-a", 9)), ("init-a.p1-3", ("init-a.p1", 3)), ("init-a", ("", -1)), ("", ("", -1)), (None, ("", -1))],
)
def test_a_run_is_its_series_and_its_number(run, series) -> None:
    assert workstore.run_series(run) == series


def test_the_mark_holds_the_newest_number_of_every_series_of_the_initiative_only() -> None:
    runs = {"init-a-9", "init-a-10", "init-a-2", "init-a.p1-3", "init-a.p1-1", "init-ab-40", "init-ab.p1-7", "other-77"}
    assert workstore.fresh_mark("init-a", runs) == {"init-a": 10, "init-a.p1": 3}
    assert workstore.fresh_mark("init-c", runs) == {}


def test_the_cutoff_merges_the_task_and_initiative_marks_series_by_series() -> None:
    task = {workstore.FRESH_AFTER_RUN: {"init-a": 9}}
    initiative = {workstore.FRESH_AFTER_RUN: {"init-a": 7, "init-a.p1": 2}}
    assert workstore.fresh_cutoff(task, initiative) == {"init-a": 9, "init-a.p1": 2}
    assert workstore.fresh_cutoff(None, initiative) == {"init-a": 7, "init-a.p1": 2}
    assert workstore.fresh_cutoff({}, None) == {}


def test_a_run_counts_only_after_the_mark_of_its_own_series() -> None:
    cutoff = {"init-a": 9, "init-a.p1": 2}
    assert not workstore.counts_after("init-a-9", cutoff)
    assert workstore.counts_after("init-a-10", cutoff)
    # A phase run is numbered in its own series: its number is never compared with the initiative's.
    assert not workstore.counts_after("init-a.p1-2", cutoff)
    assert workstore.counts_after("init-a.p1-3", cutoff)
    # A series the mark never saw began after it.
    assert workstore.counts_after("init-a.p2-1", {"init-a": 9})
    assert workstore.counts_after("init-a.p1-1", {"init-a": 9})
    # An old phase run numbered above the initiative's mark is still before its own series' mark.
    assert not workstore.counts_after("init-a.p1-12", {"init-a": 9, "init-a.p1": 12})
    # A run with no number cannot be placed after a mark.
    assert not workstore.counts_after("", cutoff)
    assert workstore.counts_after("", {})


def test_a_triage_flag_names_its_run() -> None:
    assert workstore.triage_flag_run("triage:init-a-unit:init-a-9") == "init-a-9"
    assert workstore.triage_flag_run("triage:init-a-unit:init-a.p1-2") == "init-a.p1-2"
    assert workstore.triage_flag_run("triage") == ""
