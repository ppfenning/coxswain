from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from agent_tools import cli, jsonl_log

ROW = {"run": "r1", "task": "a", "exit": 0, "steps_reached": ["merge"]}


def _merged(task: str) -> str:
    return json.dumps({"run": "r1", "task": task, "exit": 0, "steps_reached": ["merge"]})


def test_append_land_log_adds_one_complete_line(tmp_path: Path) -> None:
    cli._append_land_log(tmp_path, ROW)
    assert (tmp_path / "land.jsonl").read_bytes() == (json.dumps(ROW) + "\n").encode()


def test_append_land_log_is_one_write_and_an_fsync(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    real_write, real_fsync = os.write, os.fsync
    monkeypatch.setattr(jsonl_log.os, "write", lambda fd, data: (calls.append("write"), real_write(fd, data))[1])
    monkeypatch.setattr(jsonl_log.os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1])
    cli._append_land_log(tmp_path, ROW)
    assert calls == ["write", "fsync"]


def test_append_land_log_closes_a_torn_tail(tmp_path: Path) -> None:
    (tmp_path / "land.jsonl").write_bytes(b'{"run": "r1", "ta')
    cli._append_land_log(tmp_path, ROW)
    parsed = jsonl_log.read_log(tmp_path / "land.jsonl")
    assert (parsed.rows, parsed.skipped) == ((ROW,), 1)


def _stats_args(tmp_path: Path, runs_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        runs_dir=str(runs_dir), since=None, session=[], projects_dir=str(tmp_path / "projects"),
        work_store_root=str(tmp_path / "work"), json=True,
    )


def test_stats_chair_reads_good_rows_around_damaged_land_lines(tmp_path: Path, monkeypatch, capsys) -> None:
    runs_dir = tmp_path / "runs"
    for task in ("a", "b", "c"):
        record = runs_dir / "r1" / "tasks" / "build" / f"{task}.json"
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text('{"landed": true}')
    merged_with_nuls = _merged("b")[:-1] + "\x00\x00}"
    lines = [_merged("a"), "\x00" * 12, "null", merged_with_nuls]
    (runs_dir / "land.jsonl").write_bytes("\n".join(lines).encode() + b"\n\xff\xfe\n")
    monkeypatch.setattr(cli, "_chair_catalog_prices", lambda a: {})
    assert cli._stats_chair(_stats_args(tmp_path, runs_dir)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["hand_finished"]["n"] == 1
