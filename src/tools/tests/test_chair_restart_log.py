from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from agent_tools.chair_restart_log import (
    RestartRecord,
    bump,
    read_record,
    record_path,
    restart_note,
    with_note,
    write_record,
)
from agent_tools.dash_chair_beat import parse_status_line

NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
LINE = "chair 11-06 07:00 EST | lanes 1/3 | holding | needs chair: none"


def _ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat()


def test_bump_keeps_only_the_last_24_hours():
    prev = RestartRecord(times=(_ago(25), _ago(23)), reason="old")
    assert bump(prev, "new", NOW) == RestartRecord(times=(_ago(23), NOW.isoformat()), reason="new")


def test_bump_without_a_record_starts_one():
    assert bump(None, "boot", NOW) == RestartRecord(times=(NOW.isoformat(),), reason="boot")


def test_record_is_frozen():
    with pytest.raises(FrozenInstanceError):
        bump(None, "boot", NOW).reason = "x"  # type: ignore[misc]


def test_note_is_empty_with_no_record():
    assert restart_note(None, NOW) == ""


def test_note_is_empty_when_every_restart_is_stale():
    assert restart_note(RestartRecord(times=(_ago(30),), reason="old"), NOW) == ""


def test_note_carries_count_and_reason():
    record = RestartRecord(times=(_ago(2), _ago(1), _ago(30)), reason="code changed\nagain")
    assert restart_note(record, NOW) == "restarts 2 in 24h, last: code changed again"


def test_with_note_leaves_an_empty_note_alone():
    assert with_note(LINE, "") == LINE


def test_with_note_refuses_a_tick_error_note():
    assert with_note(LINE, "x | tick error: boom") == LINE


def test_note_bearing_status_line_still_parses():
    line = with_note(LINE, "restarts 14 in 24h, last: code changed")
    assert line == LINE + " | restarts 14 in 24h, last: code changed"
    assert parse_status_line(line) == ("11-06 07:00 EST", line)


def test_malformed_and_missing_files_read_as_none(tmp_path):
    path = record_path(tmp_path)
    assert path.name == "chair-restarts.json"
    assert read_record(path) is None
    for body in ("{not json", "[]", '{"times": [1], "reason": "r"}', '{"times": []}'):
        path.write_text(body)
        assert read_record(path) is None


def test_write_then_read_round_trips(tmp_path):
    path = record_path(tmp_path)
    record = bump(None, "boot", NOW)
    write_record(path, record)
    assert read_record(path) == record
    assert [p.name for p in tmp_path.iterdir()] == ["chair-restarts.json"]
