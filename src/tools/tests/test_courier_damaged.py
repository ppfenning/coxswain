import json
import os

from agent_tools.courier import Reference, ack_file, append_file, entries, inbox, send, skipped

_ONE = send(Reference("run", "r1"), "a", "cos", "one", "m1")
_TWO = send(Reference("run", "r2"), "a", "cos", "two", "m2")


def _line(entry: dict) -> str:
    return json.dumps(entry) + "\n"


def _with_nuls(entry: dict) -> str:
    text = json.dumps(entry)
    return text[:10] + "\x00\x00" + text[10:] + "\n"


def test_an_all_null_line_is_skipped_and_counted():
    blob = _line(_ONE) + "\x00\x00\x00\n" + _line(_TWO)
    assert entries(blob) == [_ONE, _TWO]
    assert inbox(blob) == [_ONE, _TWO]
    assert skipped(blob) == 1


def test_an_entry_line_with_embedded_nuls_is_recovered():
    blob = _line(_ONE) + _with_nuls(_TWO)
    assert entries(blob) == [_ONE, _TWO]
    assert inbox(blob) == [_ONE, _TWO]
    assert skipped(blob) == 0


def test_a_clean_blob_reports_no_skipped_lines():
    assert skipped(_line(_ONE)) == 0


def test_ack_file_acks_past_an_all_null_line(tmp_path):
    path = tmp_path / "courier.jsonl"
    path.write_text(_line(_ONE) + "\x00\x00\x00\n" + _line(_TWO), encoding="utf-8")
    assert ack_file(path, "m2")
    assert [e["ack"] for e in entries(path.read_text(encoding="utf-8"))] == [False, True]


def test_ack_file_acks_an_entry_whose_line_holds_nuls(tmp_path):
    path = tmp_path / "courier.jsonl"
    path.write_text(_with_nuls(_ONE), encoding="utf-8")
    assert ack_file(path, "m1")
    assert [e["ack"] for e in entries(path.read_text(encoding="utf-8"))] == [True]


def test_append_file_closes_a_torn_line_adds_one_line_and_syncs(tmp_path, monkeypatch):
    path = tmp_path / "courier.jsonl"
    path.write_text(_line(_ONE) + '{"ref": "co', encoding="utf-8")
    before = len(path.read_text(encoding="utf-8").splitlines())
    synced: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    append_file(path, _TWO)
    text = path.read_text(encoding="utf-8")
    assert len(text.splitlines()) == before + 1
    assert text.endswith(_line(_TWO)) and text.count('{"ref": "co\n') == 1
    assert len(synced) == 1
