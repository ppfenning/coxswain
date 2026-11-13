import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_tools.chair_read_idle_lands import read_lands_waiting
from agent_tools.chair_read_land_conflicts import read_land_conflicts

_NOW = datetime(2026, 10, 5, 2, 0, tzinfo=UTC)
_IDS = {("/r", "epic/alpha/p2"): "b1", ("/r", "main"): "m1"}
_QUEUED = ({"check_runs": [{"status": "queued"}]}, {"statuses": []})

Reader = Callable[[Path], list]


def _wait_row(n: int) -> dict:
    pr = f"https://github.com/o/r/pull/{n}"
    return {"run": f"r{n}", "task": "t", "pr": pr, "steps_reached": ["pr_create"], "ts": f"2026-10-05T01:0{n}:00+00:00"}


def _squash_row(n: int) -> dict:
    return {
        "ts": f"2026-10-13T10:0{n}:00+00:00",
        "run": f"alpha-{n}",
        "stopped_step": "squash_phase",
        "stop_detail": "squash of epic/alpha/p2 conflicts in: a.py",
        "conflict_paths": [f"{n}.py"],
        "branch_sha": "b1",
        "from_sha": "m1",
    }


def _waits(runs_dir: Path) -> list:
    return read_lands_waiting(runs_dir, _NOW, lambda pr, timeout_s: _QUEUED)


def _conflicts(runs_dir: Path) -> list:
    return read_land_conflicts(str(runs_dir), {"alpha": "/r"}, lambda repo, ref: _IDS.get((repo, ref)))


def _write(directory: Path, lines: list[bytes]) -> Path:
    directory.mkdir(exist_ok=True)
    (directory / "land.jsonl").write_bytes(b"\n".join(lines) + b"\n")
    return directory


def _encoded(row: dict) -> bytes:
    return json.dumps(row).encode()


def _with_nuls(row: dict) -> bytes:
    raw = _encoded(row)
    return raw[:12] + b"\x00\x00" + raw[12:]


@pytest.mark.parametrize("reader,make", [(_waits, _wait_row), (_conflicts, _squash_row)])
def test_damaged_lines_are_skipped_and_the_other_rows_come_back_as_before(tmp_path, reader: Reader, make) -> None:
    clean = _write(tmp_path / "clean", [_encoded(make(1)), _encoded(make(2)), _encoded(make(3))])
    damaged = _write(tmp_path / "damaged", [_encoded(make(1)), b"null", _encoded(make(2)), _with_nuls(make(3))])
    assert len(reader(clean)) == 3
    assert reader(damaged) == reader(clean)


@pytest.mark.parametrize("reader,make", [(_waits, _wait_row), (_conflicts, _squash_row)])
def test_undecodable_bytes_do_not_raise_and_cost_only_their_line(tmp_path, reader: Reader, make) -> None:
    clean = _write(tmp_path / "clean", [_encoded(make(1)), _encoded(make(2))])
    damaged = _write(tmp_path / "damaged", [_encoded(make(1)), b"\xff\xfe\x80 not utf-8", _encoded(make(2))])
    assert reader(damaged) == reader(clean) != []
