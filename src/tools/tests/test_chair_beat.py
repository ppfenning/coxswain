from __future__ import annotations

import datetime
import threading
import time
from collections.abc import Callable

from agent_tools import chair
from agent_tools.chair_beat import beat_in_thread

INTERVAL = 0.01
_T0 = datetime.datetime(2026, 11, 3, 12, 0, tzinfo=datetime.UTC)


def _taken(hours: float | None = 2) -> dict:
    record, _ = chair.take(None, "loop", 7, "h", _T0, 5, True, hours=hours)
    assert record is not None
    return record


def test_beat_with_window_moves_until_to_beat_time_plus_window() -> None:
    taken = _taken()
    later = _T0 + datetime.timedelta(minutes=5)
    beaten, reason = chair.beat(taken, "loop", 7, "h", later, extend_window=True)
    assert reason == ""
    assert beaten["until"] > taken["until"]
    assert beaten["until"] == (later + datetime.timedelta(hours=2)).isoformat()


def test_second_beat_moves_until_forward_again() -> None:
    first, _ = chair.beat(_taken(), "loop", 7, "h", _T0 + datetime.timedelta(minutes=5), extend_window=True)
    later = _T0 + datetime.timedelta(minutes=20)
    second, _ = chair.beat(first, "loop", 7, "h", later, extend_window=True)
    assert second["until"] > first["until"]
    assert second["until"] == (later + datetime.timedelta(hours=2)).isoformat()


def test_beat_without_flag_leaves_until() -> None:
    taken = _taken()
    beaten, _ = chair.beat(taken, "loop", 7, "h", _T0 + datetime.timedelta(minutes=5))
    assert beaten["until"] == taken["until"]


def test_window_beat_on_record_without_until_adds_none() -> None:
    beaten, _ = chair.beat(_taken(None), "loop", 7, "h", _T0 + datetime.timedelta(minutes=5), extend_window=True)
    assert "until" not in beaten


def test_window_beat_with_malformed_taken_at_leaves_until() -> None:
    taken = {**_taken(), "taken_at": "not-a-time"}
    beaten, _ = chair.beat(taken, "loop", 7, "h", _T0 + datetime.timedelta(minutes=5), extend_window=True)
    assert beaten["until"] == taken["until"]


def _minutes(n: int) -> datetime.datetime:
    return _T0 + datetime.timedelta(minutes=n)


def _legacy(record: dict) -> dict:
    return {k: v for k, v in record.items() if k != "window_seconds"}


def test_extend_after_a_flagged_beat_sets_the_window_later_beats_use() -> None:
    first, _ = chair.beat(_taken(), "loop", 7, "h", _minutes(5), extend_window=True)
    extended = chair.extend(first, _minutes(10), 8)
    later, _ = chair.beat(extended, "loop", 7, "h", _minutes(15), extend_window=True)
    assert later["until"] == (_minutes(15) + datetime.timedelta(hours=8)).isoformat()


def test_extend_on_a_long_held_lease_does_not_inflate_the_window() -> None:
    held, _ = chair.take(None, "loop", 7, "h", _T0, 5, True, hours=8)
    extended = chair.extend(held, _minutes(360), 2)
    beaten, _ = chair.beat(extended, "loop", 7, "h", _minutes(365), extend_window=True)
    assert beaten["until"] == (_minutes(365) + datetime.timedelta(hours=2)).isoformat()


def test_extend_of_an_open_ended_lease_sets_the_window_for_later_beats() -> None:
    held = _taken(None)
    extended = chair.extend(held, _minutes(60), 2)
    beaten, _ = chair.beat(extended, "loop", 7, "h", _minutes(65), extend_window=True)
    assert beaten["until"] == (_minutes(65) + datetime.timedelta(hours=2)).isoformat()


def test_unflagged_beat_after_a_flagged_one_keeps_until_and_window() -> None:
    first, _ = chair.beat(_taken(), "loop", 7, "h", _minutes(5), extend_window=True)
    plain, _ = chair.beat(first, "loop", 7, "h", _minutes(10))
    assert (plain["until"], plain["window_seconds"]) == (first["until"], first["window_seconds"])


def test_record_without_window_seconds_derives_it_once_and_keeps_it_constant() -> None:
    first, _ = chair.beat(_legacy(_taken()), "loop", 7, "h", _minutes(5), extend_window=True)
    second, _ = chair.beat(first, "loop", 7, "h", _minutes(20), extend_window=True)
    assert second["until"] == (_minutes(20) + datetime.timedelta(hours=2)).isoformat()


def test_span_that_is_not_positive_leaves_until() -> None:
    backwards = {**_legacy(_taken()), "until": (_T0 - datetime.timedelta(hours=1)).isoformat()}
    assert chair.beat(backwards, "loop", 7, "h", _minutes(5), extend_window=True)[0]["until"] == backwards["until"]


def test_non_finite_or_absurd_window_seconds_leave_until() -> None:
    for bad in (float("inf"), float("nan"), -5, 0, 1e12):
        record = {**_taken(), "window_seconds": bad}
        assert chair.beat(record, "loop", 7, "h", _minutes(5), extend_window=True)[0]["until"] == record["until"]


def test_window_beat_by_other_pid_returns_none() -> None:
    assert chair.beat(_taken(), "loop", 8, "h", _T0, extend_window=True) == (None, "chair: not held by loop (pid 8) on h")


def _wait_until(cond: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def test_beats_continue_while_main_thread_blocks() -> None:
    calls: list[int] = []
    stop = threading.Event()
    thread = beat_in_thread(lambda: calls.append(1), INTERVAL, stop, lambda exc: None)
    try:
        threading.Event().wait(0.2)
        assert len(calls) >= 3
        assert thread.daemon is True
    finally:
        stop.set()
        thread.join(timeout=1)


def test_stop_ends_thread_and_count_stops_rising() -> None:
    calls: list[int] = []
    stop = threading.Event()
    thread = beat_in_thread(lambda: calls.append(1), INTERVAL, stop, lambda exc: None)
    try:
        assert _wait_until(lambda: len(calls) >= 1)
    finally:
        stop.set()
        thread.join(timeout=1)
    assert not thread.is_alive()
    settled = len(calls)
    threading.Event().wait(INTERVAL * 5)
    assert len(calls) == settled


def test_stop_is_prompt_with_a_long_interval() -> None:
    stop = threading.Event()
    thread = beat_in_thread(lambda: None, 30.0, stop, lambda exc: None)
    stop.set()
    thread.join(timeout=1)
    assert not thread.is_alive()


def test_raising_beat_is_recorded_and_later_beats_still_run() -> None:
    calls: list[int] = []
    errors: list[Exception] = []

    def beat() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("first beat fails")

    stop = threading.Event()
    thread = beat_in_thread(beat, INTERVAL, stop, errors.append)
    try:
        assert _wait_until(lambda: len(calls) >= 3)
        assert thread.is_alive()
    finally:
        stop.set()
        thread.join(timeout=1)
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
