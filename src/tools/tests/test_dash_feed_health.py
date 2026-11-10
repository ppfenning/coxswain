"""The live feed survives damaged lines in courier.jsonl, land.jsonl and chair.actions.jsonl and counts them in `health`."""

import json
from pathlib import Path

from agent_tools import dash_feed

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "dash_feed_v1.json").read_text())
_NOW = "2026-11-10T00:00:00Z"
_BUS = {"from": "chair", "to": "operator", "ack": False, "options": ["ship", "hold"]}
_ASK = _BUS | {"ref": "coxswain://decision/d-1", "id": "d-1", "note": "Ship it?", "context": "c", "asked_at": "2026-11-09T20:00:00Z"}
_CLOSED = _BUS | {"ref": "coxswain://decision/d-2", "id": "d-2", "note": "Hold it?", "context": "c", "asked_at": "2026-11-09T19:00:00Z"}
_ANSWER = {"ref": "coxswain://decision/d-2", "id": "a-2", "from": "operator", "to": "chair", "note": "answer: hold",
           "ack": False, "answer": "hold", "asked_at": "2026-11-09T20:30:00Z"}
_DAMAGE = "null\n" + "\x00\x00\x00\x00\n"


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows[:1]) + _DAMAGE + "".join(json.dumps(row) + "\n" for row in rows[1:]))


def _feed(runs_dir: Path) -> dict:
    return json.loads(json.dumps(dash_feed.gather_feed(runs_dir, runs_dir, _NOW)))


def test_damaged_lines_in_all_three_logs_do_not_stop_the_feed_and_are_counted(tmp_path):
    _write(tmp_path / "courier.jsonl", [_ASK, _CLOSED, _ANSWER])
    _write(tmp_path / "land.jsonl", [{"run": "r1", "exit": 0}, {"run": "r2", "exit": 0}])
    _write(tmp_path / "chair.actions.jsonl", [{"at": _NOW, "action": "tick"}])

    feed = _feed(tmp_path)

    assert [d["id"] for d in feed["decisions"]] == ["d-1"]
    assert feed["health"] == {
        "skipped_total": 6,
        "logs": [
            {"log": "courier.jsonl", "skipped": 2},
            {"log": "land.jsonl", "skipped": 2},
            {"log": "chair.actions.jsonl", "skipped": 2},
        ],
    }


def test_a_clean_directory_yields_the_zero_block(tmp_path):
    assert _feed(tmp_path)["health"] == {"skipped_total": 0, "logs": []}


def test_invalid_utf8_in_the_courier_log_does_not_raise(tmp_path):
    (tmp_path / "courier.jsonl").write_bytes(json.dumps(_ASK).encode() + b"\n\xff\xfe\n")

    feed = _feed(tmp_path)

    assert [d["id"] for d in feed["decisions"]] == ["d-1"]
    assert feed["health"]["logs"] == [{"log": "courier.jsonl", "skipped": 1}]


def test_the_live_feed_keys_match_the_fixture(tmp_path):
    assert set(_feed(tmp_path)) >= set(_FIXTURE) - {"orchestration", "operator_sessions", "schedule"}
    assert set(_feed(tmp_path)["health"]) == set(_FIXTURE["health"])
