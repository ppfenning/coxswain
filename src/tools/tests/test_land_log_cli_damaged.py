import json
import os

import pytest

from agent_tools import cli, jsonl_log

_ROW = {"run": "r1", "task": "t01", "exit": 0}


def _line(row: dict) -> bytes:
    return json.dumps(row).encode() + b"\n"


def test_append_adds_exactly_one_complete_line(tmp_path):
    cli._append_land_log(tmp_path, _ROW)
    assert (tmp_path / "land.jsonl").read_bytes() == _line(_ROW)


def test_append_is_one_write_and_one_fsync(tmp_path, monkeypatch):
    writes, syncs = [], []
    real_write, real_fsync = os.write, os.fsync
    monkeypatch.setattr(jsonl_log.os, "write", lambda fd, data: writes.append(data) or real_write(fd, data))
    monkeypatch.setattr(jsonl_log.os, "fsync", lambda fd: syncs.append(fd) or real_fsync(fd))
    cli._append_land_log(tmp_path, _ROW)
    assert writes == [_line(_ROW)]
    assert len(syncs) == 1


def test_append_closes_a_torn_tail(tmp_path):
    (tmp_path / "land.jsonl").write_bytes(b'{"run": "r0", "ta')
    cli._append_land_log(tmp_path, _ROW)
    assert (tmp_path / "land.jsonl").read_bytes() == b'{"run": "r0", "ta\n' + _line(_ROW)


@pytest.fixture
def chair_rows(monkeypatch):
    seen = []
    monkeypatch.setattr(cli.stats_chair, "chair_report", lambda *args: seen.append(args[3]) or {})
    return seen


def _run_stats_chair(tmp_path):
    argv = ["stats", "chair", str(tmp_path / "runs"), "--json", "--work-store-root", str(tmp_path / "work"),
            "--projects-dir", str(tmp_path / "projects"), "--profile", str(tmp_path / "profile.yaml")]
    assert cli.main(argv) == 0


def _seed(tmp_path, data: bytes):
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "land.jsonl").write_bytes(data)


def test_reader_returns_good_rows_around_null_damage(tmp_path, chair_rows):
    all_null = b"\x00" * 40 + b"\n"
    embedded = b'{"run":\x00 "r2", "exit": 0}\n'
    _seed(tmp_path, _line({"run": "r1", "exit": 0}) + all_null + embedded + _line({"run": "r3", "exit": 0}))
    _run_stats_chair(tmp_path)
    assert chair_rows == [[{"run": "r1", "exit": 0}, {"run": "r2", "exit": 0}, {"run": "r3", "exit": 0}]]


def test_reader_replaces_undecodable_bytes(tmp_path, chair_rows):
    _seed(tmp_path, b'{"run": "r1"}\n\xff\xfe\n{"run": "r2"}\n')
    _run_stats_chair(tmp_path)
    assert chair_rows == [[{"run": "r1"}, {"run": "r2"}]]


def test_reader_gives_no_rows_for_a_missing_file(tmp_path, chair_rows):
    (tmp_path / "runs").mkdir()
    _run_stats_chair(tmp_path)
    assert chair_rows == [[]]
