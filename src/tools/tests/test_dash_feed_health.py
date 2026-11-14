import json

import pytest

from agent_tools import dash_feed
from agent_tools.chair_read_record import ACTION_LOG

_NOW = "2026-09-29T21:00:00Z"
_DAMAGE = "null\n" + "ab\x00\x00cd\x00\n"
_ASK = {
    "ref": "coxswain://decision/d-1", "id": "d-1", "from": "chair", "to": "operator", "ack": False,
    "options": ["ship", "hold"], "context": "c", "note": "Ship it?", "asked_at": "2026-09-29T20:00:00Z",
}


@pytest.fixture(autouse=True)
def _no_real_claude(monkeypatch):
    monkeypatch.setattr(dash_feed, "_local_login", lambda local_name: None)
    monkeypatch.setattr(dash_feed.run_store, "initiative_short_ids", lambda runs_dir: {})


def _lines(*rows) -> str:
    return "".join(json.dumps(row) + "\n" for row in rows)


def _dirs(tmp_path):
    work, runs = tmp_path / "work", tmp_path / "runs"
    work.mkdir()
    runs.mkdir()
    return work, runs


def _write_damaged(work, runs) -> None:
    (work / "courier.jsonl").write_text(_lines(_ASK) + _DAMAGE, encoding="utf-8")
    (runs / "land.jsonl").write_text(_lines({"pr": 1}) + _DAMAGE, encoding="utf-8")
    (runs / ACTION_LOG).write_text(_lines({"action": "tick"}) + _DAMAGE, encoding="utf-8")


def test_the_feed_builds_and_keeps_the_open_decision_from_the_good_lines(tmp_path):
    work, runs = _dirs(tmp_path)
    _write_damaged(work, runs)

    feed = dash_feed.gather_feed(runs, work, _NOW)

    assert [d["id"] for d in feed["decisions"]] == ["d-1"]


def test_health_lists_the_three_damaged_logs_in_order_with_the_total(tmp_path):
    work, runs = _dirs(tmp_path)
    _write_damaged(work, runs)

    assert dash_feed.gather_feed(runs, work, _NOW)["health"] == {
        "skipped_total": 6,
        "logs": [
            {"log": "courier.jsonl", "skipped": 2},
            {"log": "land.jsonl", "skipped": 2},
            {"log": "chair.actions.jsonl", "skipped": 2},
        ],
    }


def test_health_names_only_the_logs_with_damage(tmp_path):
    work, runs = _dirs(tmp_path)
    (work / "courier.jsonl").write_text(_lines(_ASK), encoding="utf-8")
    (runs / "land.jsonl").write_text(_DAMAGE, encoding="utf-8")

    assert dash_feed._health(work, runs) == {"skipped_total": 2, "logs": [{"log": "land.jsonl", "skipped": 2}]}


def test_a_clean_directory_yields_the_zero_block(tmp_path):
    work, runs = _dirs(tmp_path)

    assert dash_feed.gather_feed(runs, work, _NOW)["health"] == {"skipped_total": 0, "logs": []}


def test_a_courier_log_with_invalid_utf8_does_not_raise(tmp_path):
    work, runs = _dirs(tmp_path)
    (work / "courier.jsonl").write_bytes(_lines(_ASK).encode() + b"\xff\xfe\x00\n")

    feed = dash_feed.gather_feed(runs, work, _NOW)

    assert [d["id"] for d in feed["decisions"]] == ["d-1"]
    assert feed["health"]["logs"] == [{"log": "courier.jsonl", "skipped": 1}]
