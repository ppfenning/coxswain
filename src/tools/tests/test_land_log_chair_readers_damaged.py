import json
from datetime import UTC, datetime
from pathlib import Path

from agent_tools.chair_read_idle_lands import read_lands_waiting
from agent_tools.chair_read_land_conflicts import read_land_conflicts

_NOW = datetime(2026, 10, 5, 2, 0, tzinfo=UTC)
_NULS = b"\x00" * 12
_IDS = {("/r", "epic/alpha/p2"): "b1", ("/r", "main"): "m1"}


def _write(tmp_path: Path, good: list[dict], extra: list[bytes]) -> str:
    lines = [json.dumps(r).encode() for r in good]
    (tmp_path / "land.jsonl").write_bytes(b"\n".join([lines[0], *extra, *lines[1:]]) + b"\n")
    return str(tmp_path)


def _squash(run: str, phase: str) -> dict:
    return {
        "ts": "2026-10-13T10:00:00+00:00", "run": run, "stopped_step": "squash_phase",
        "stop_detail": f"squash of epic/{run.rsplit('-', 1)[0]}/{phase} conflicts in: a.py",
        "conflict_paths": ["a.py"], "branch_sha": "b1", "from_sha": "m1",
    }


def _open(n: int) -> dict:
    pr = f"https://github.com/o/r/pull/{n}"
    return {"run": f"r{n}", "task": "t", "pr": pr, "steps_reached": ["pr_create"], "ts": "2026-10-05T01:00:00+00:00"}


def _queued(pr: str, timeout_s: float) -> tuple[dict, dict]:
    return {"total_count": 1, "check_runs": [{"name": "c", "status": "queued"}]}, {"total_count": 0, "statuses": []}


def _conflicts(runs_dir: str) -> list[dict]:
    return read_land_conflicts(runs_dir, {"alpha": "/r"}, lambda repo, ref: _IDS.get((repo, ref)))  # type: ignore[return-value]


def test_conflicts_reader_skips_a_null_line_and_an_all_nul_line(tmp_path: Path) -> None:
    good = [_squash("alpha-3", "p2"), _squash("alpha-4", "p2")]
    (tmp_path / "clean").mkdir()
    clean = _conflicts(_write(tmp_path / "clean", good, []))
    got = _conflicts(_write(tmp_path, good, [b"null", _NULS]))
    assert got == clean
    assert [c["run"] for c in got] == ["alpha-3", "alpha-4"]


def test_conflicts_reader_recovers_a_row_with_embedded_nuls(tmp_path: Path) -> None:
    damaged = json.dumps(_squash("alpha-9", "p2")).encode().replace(b"squash_phase", b"squash\x00_phase")
    got = _conflicts(_write(tmp_path, [_squash("alpha-3", "p2")], [damaged]))
    assert sorted(c["run"] for c in got) == ["alpha-3", "alpha-9"]


def test_conflicts_reader_does_not_raise_on_undecodable_bytes(tmp_path: Path) -> None:
    got = _conflicts(_write(tmp_path, [_squash("alpha-3", "p2")], [b"\xff\xfe{", _NULS]))
    assert [c["run"] for c in got] == ["alpha-3"]


def test_waiting_reader_skips_a_null_line_and_an_all_nul_line(tmp_path: Path) -> None:
    good = [_open(1), _open(2)]
    got = read_lands_waiting(Path(_write(tmp_path, good, [b"null", _NULS])), _NOW, _queued)
    assert sorted(w["run"] for w in got) == ["r1", "r2"]
    assert all(w["checks_started"] is False for w in got)


def test_waiting_reader_recovers_a_row_with_embedded_nuls(tmp_path: Path) -> None:
    damaged = json.dumps(_open(3)).encode().replace(b'"r3"', b'"r\x003"')
    got = read_lands_waiting(Path(_write(tmp_path, [_open(1)], [damaged])), _NOW, _queued)
    assert sorted(w["run"] for w in got) == ["r1", "r3"]


def test_waiting_reader_does_not_raise_on_undecodable_bytes(tmp_path: Path) -> None:
    got = read_lands_waiting(Path(_write(tmp_path, [_open(1)], [b"\xff\xfe{", _NULS])), _NOW, _queued)
    assert [w["run"] for w in got] == ["r1"]
