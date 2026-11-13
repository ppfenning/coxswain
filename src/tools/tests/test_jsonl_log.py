import json
import os
from pathlib import Path

import pytest

from agent_tools import jsonl_log
from agent_tools.jsonl_log import ParsedLog, append_line, parse_log, read_log


def test_all_null_line_is_damaged():
    assert parse_log("\x00\x00\x00\n") == ParsedLog((), 1, ("\x00\x00\x00",))


def test_embedded_nuls_that_parse_once_stripped_are_a_row():
    assert parse_log('{"a"\x00: 1\x00}\n') == ParsedLog(({"a": 1},), 0, ())


def test_unparseable_line_keeps_its_raw_text():
    assert parse_log('{"a": 1}\n{"a": \x00\n') == ParsedLog(({"a": 1},), 1, ('{"a": \x00',))


@pytest.mark.parametrize("line", ["[1]", "3", '"s"', "null"])
def test_non_dict_json_is_skipped(line):
    assert parse_log(line + "\n") == ParsedLog((), 1, (line,))


def test_blank_lines_are_not_counted():
    assert parse_log('\n  \t\n{"a": 1}\r\n\n') == ParsedLog(({"a": 1},), 0, ())


def test_missing_file_gives_empty_log(tmp_path: Path):
    assert read_log(tmp_path / "absent.jsonl") == ParsedLog((), 0, ())


def test_read_log_replaces_invalid_utf8(tmp_path: Path):
    path = tmp_path / "log.jsonl"
    path.write_bytes(b'{"a": "\xff"}\n')
    assert read_log(path).rows == ({"a": "�"},)


def test_torn_tail_is_closed_by_next_append(tmp_path: Path):
    path = tmp_path / "log.jsonl"
    path.write_bytes(b'{"a": 1}\n{"b":')
    append_line(path, {"c": 3}, sync=False)
    assert path.read_bytes() == b'{"a": 1}\n{"b":\n{"c": 3}\n'
    assert read_log(path) == ParsedLog(({"a": 1}, {"c": 3}), 1, ('{"b":',))


def test_append_to_new_file_and_clean_tail_add_no_leading_newline(tmp_path: Path):
    path = tmp_path / "log.jsonl"
    append_line(path, {"a": 1}, sync=False)
    append_line(path, {"b": 2}, sync=False)
    assert path.read_text() == json.dumps({"a": 1}) + "\n" + json.dumps({"b": 2}) + "\n"


@pytest.mark.parametrize("seed", [b"", b'{"a": 1}\n', b'{"a":'])
def test_append_issues_exactly_one_write(tmp_path: Path, monkeypatch, seed: bytes):
    path = tmp_path / "log.jsonl"
    path.write_bytes(seed)
    real_write = os.write
    calls: list[bytes] = []

    def recording_write(fd: int, data: bytes) -> int:
        calls.append(data)
        return real_write(fd, data)

    monkeypatch.setattr(jsonl_log.os, "write", recording_write)
    append_line(path, {"z": 9}, sync=False)
    assert len(calls) == 1
    assert calls[0].endswith(b'{"z": 9}\n')


@pytest.mark.parametrize("sync,expected", [(True, 1), (False, 0)])
def test_fsync_only_when_sync(tmp_path: Path, monkeypatch, sync: bool, expected: int):
    synced: list[int] = []
    monkeypatch.setattr(jsonl_log.os, "fsync", synced.append)
    append_line(tmp_path / "log.jsonl", {"a": 1}, sync=sync)
    assert len(synced) == expected
