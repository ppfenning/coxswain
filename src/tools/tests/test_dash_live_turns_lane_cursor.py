from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_tools import dash_live_turns_lane as lane
from agent_tools.dash_live_turns_lane import advance_lane_cursor, read_lane_turns

PRICES = {"m1": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75}}
ARGS = ("run1", "host1", "init", "task", "build")


def _event(message_id: str, output: int = 200, minute: int = 0) -> str:
    usage = {"input_tokens": 10, "output_tokens": output}
    event = {
        "timestamp": f"2026-11-06T11:{minute:02d}:00Z",
        "message": {"id": message_id, "model": "m1", "usage": usage},
    }
    return json.dumps(event) + "\n"


@pytest.fixture
def trace(tmp_path: Path) -> Path:
    directory = tmp_path / "run1-trace"
    directory.mkdir()
    path = directory / "build-1.jsonl"
    path.write_text(_event("a", minute=1) + _event("b", minute=2))
    return path


@pytest.fixture
def bytes_read(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    seen: list[int] = []
    real = lane._read_from

    def counting(path: Path, offset: int) -> bytes:
        chunk = real(path, offset)
        seen.append(len(chunk))
        return chunk

    monkeypatch.setattr(lane, "_read_from", counting)
    return seen


def _turns(tmp_path: Path, cursor=None):
    return read_lane_turns(tmp_path, *ARGS, PRICES, cursor=cursor)


def test_unchanged_trace_reads_nothing(tmp_path, trace, bytes_read):
    cursor = advance_lane_cursor(tmp_path, "run1", "build")
    first = _turns(tmp_path, cursor)
    bytes_read.clear()
    assert advance_lane_cursor(tmp_path, "run1", "build", cursor) == cursor
    assert _turns(tmp_path, cursor) == first
    assert bytes_read == []
    assert len(first) == 2


def test_appended_event_reads_only_new_bytes(tmp_path, trace, bytes_read):
    cursor = advance_lane_cursor(tmp_path, "run1", "build")
    before = _turns(tmp_path, cursor)
    added = _event("c", minute=3)
    with trace.open("a") as handle:
        handle.write(added)
    bytes_read.clear()
    grown = advance_lane_cursor(tmp_path, "run1", "build", cursor)
    assert bytes_read == [len(added)]
    assert len(_turns(tmp_path, grown)) == len(before) + 1


def test_incremental_equals_full_read(tmp_path, trace):
    cursor = advance_lane_cursor(tmp_path, "run1", "build")
    with trace.open("a") as handle:
        handle.write(_event("a", output=900, minute=1) + "not json\n" + _event("c", minute=3))
    grown = advance_lane_cursor(tmp_path, "run1", "build", cursor)
    assert _turns(tmp_path, grown) == _turns(tmp_path)
    assert [t["output_tokens"] for t in _turns(tmp_path, grown)] == [200, 900, 200]


def test_unfinished_last_line_waits_for_its_newline(tmp_path, trace):
    cursor = advance_lane_cursor(tmp_path, "run1", "build")
    line = _event("c", minute=3)
    with trace.open("a") as handle:
        handle.write(line[:-1])
    partial = advance_lane_cursor(tmp_path, "run1", "build", cursor)
    assert len(_turns(tmp_path, partial)) == 2
    with trace.open("a") as handle:
        handle.write("\n")
    done = advance_lane_cursor(tmp_path, "run1", "build", partial)
    assert _turns(tmp_path, done) == _turns(tmp_path)
    assert len(_turns(tmp_path, done)) == 3
