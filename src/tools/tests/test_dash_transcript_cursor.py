import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_dash_live_turns_session import PRICES, _assistant

import agent_tools.dash_live_turns_session as session
from agent_tools.dash_live_turns_session import read_session_turns, read_session_turns_with_cursor
from agent_tools.dash_transcript_cursor import CursorState, PathCursor, advance

SINCE = datetime(2026, 11, 6, tzinfo=UTC)


def _line(n: int, ts: str = "2026-11-06T11:59:00.000Z") -> str:
    return _assistant(ts=ts, message_id=f"msg_{n}") + "\n"


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


@pytest.fixture
def transcript(tmp_path):
    path = tmp_path / "proj" / "sess.jsonl"
    path.parent.mkdir()
    path.write_text(_line(1) + _line(2))
    return path


@pytest.fixture
def reads(monkeypatch):
    """Byte counts handed back by the file reader, one per call."""
    seen: list[int] = []
    real = session._read_from

    def counting(path, offset):
        data = real(path, offset)
        seen.append(len(data))
        return data

    monkeypatch.setattr(session, "_read_from", counting)
    return seen


def _call(root, cursor):
    return read_session_turns_with_cursor(root, "host-a", "sess", SINCE, cursor, prices=PRICES)


def test_the_state_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        PathCursor(1, 2, 3, ()).offset = 4  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        CursorState().scope = "x"  # type: ignore[misc]


def test_an_unchanged_file_reads_zero_bytes(tmp_path, transcript, reads):
    first, cursor = _call(tmp_path, None)
    again, cursor2 = _call(tmp_path, cursor)
    assert again == first
    assert len(first) == 2
    assert cursor2 == cursor
    assert reads == [transcript.stat().st_size]


def test_an_appended_line_reads_only_the_new_bytes_and_yields_that_turn(tmp_path, transcript, reads):
    _, cursor = _call(tmp_path, None)
    _append(transcript, _line(3, "2026-11-06T12:30:00.000Z"))
    turns, cursor2 = _call(tmp_path, cursor)
    assert len(turns) == 3
    assert turns[-1]["ts"] == "2026-11-06T12:30:00.000Z"
    assert reads[-1] == len(_line(3))
    assert cursor2.paths[0][1].offset == transcript.stat().st_size


def test_a_duplicated_message_key_is_not_counted_twice(tmp_path, transcript):
    _, cursor = _call(tmp_path, None)
    _append(transcript, _line(1) + _line(3))
    turns, _ = _call(tmp_path, cursor)
    assert len(turns) == 3


def test_a_partial_last_line_waits_for_its_newline(tmp_path, transcript):
    _, cursor = _call(tmp_path, None)
    whole = _line(3).rstrip("\n")
    _append(transcript, whole[:20])
    turns, cursor2 = _call(tmp_path, cursor)
    assert len(turns) == 2
    assert cursor2.paths[0][1].offset == cursor.paths[0][1].offset
    _append(transcript, whole[20:] + "\n")
    turns, _ = _call(tmp_path, cursor2)
    assert len(turns) == 3


def test_a_truncated_file_resets_and_reparses(tmp_path, transcript):
    _, cursor = _call(tmp_path, None)
    transcript.write_text(_line(9))
    turns, cursor2 = _call(tmp_path, cursor)
    assert turns == read_session_turns(tmp_path, "host-a", "sess", SINCE, prices=PRICES)
    assert len(turns) == 1
    assert cursor2.paths[0][1].offset == transcript.stat().st_size


def test_a_vanished_path_drops_out(tmp_path, transcript):
    _, cursor = _call(tmp_path, None)
    transcript.unlink()
    turns, cursor2 = _call(tmp_path, cursor)
    assert (turns, cursor2.paths) == ([], ())


def test_the_incremental_result_equals_a_full_parse_after_each_append(tmp_path, transcript):
    cursor = None
    for n in range(3, 7):
        _append(transcript, _line(n, f"2026-11-06T12:{n:02d}:00.000Z"))
        turns, cursor = _call(tmp_path, cursor)
        assert turns == read_session_turns(tmp_path, "host-a", "sess", SINCE, prices=PRICES)


def test_read_session_turns_with_a_cursor_returns_the_same_turns(tmp_path, transcript):
    _, cursor = _call(tmp_path, None)
    expected = read_session_turns(tmp_path, "host-a", "sess", SINCE, prices=PRICES)
    assert read_session_turns(tmp_path, "host-a", "sess", SINCE, prices=PRICES, cursor=cursor) == expected


def test_a_cursor_built_for_other_prices_is_ignored(tmp_path, transcript):
    _, cursor = _call(tmp_path, None)
    turns, _ = read_session_turns_with_cursor(tmp_path, "host-a", "sess", SINCE, cursor, prices={})
    assert [t["cost_usd"] for t in turns] == [0.0, 0.0]


def test_advance_with_literals_keeps_an_unchanged_path_and_does_not_read():
    path = Path("a.jsonl")
    kept = PathCursor(10, 5, 10, (("k", {"ts": "t"}),))

    def read_from(_path, _offset):
        raise AssertionError("must not read")

    state = advance(CursorState(((path, kept),)), {path: (10, 5)}, read_from, lambda _p, _lines: [])
    assert state.paths == ((path, kept),)
