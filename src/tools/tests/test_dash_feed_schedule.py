"""The feed's `schedule` key is the `schedule` object of runs/schedule.json, shaped by
tests/fixtures/dash_feed_schedule_v1.json, and is absent when the document is missing or corrupt."""

import json
from pathlib import Path

from agent_tools import dash_feed
from agent_tools.chair_schedule import SCHEDULE_FILE
from agent_tools.dash_feed import snapshot

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "dash_feed_schedule_v1.json").read_text())
_SCHEDULE = _FIXTURE["schedule"]
_NOW = "2026-11-06T00:00:00Z"


def _feed(runs_dir: Path) -> dict:
    return json.loads(json.dumps(dash_feed.gather_feed(runs_dir, runs_dir, _NOW)))


def test_a_published_document_puts_its_schedule_object_in_the_feed_unchanged(tmp_path):
    (tmp_path / SCHEDULE_FILE).write_text(json.dumps({"schema": 1, "at": _NOW, "schedule": _SCHEDULE}))

    assert _feed(tmp_path)["schedule"] == _SCHEDULE


def test_no_document_leaves_the_schedule_key_out(tmp_path):
    assert "schedule" not in _feed(tmp_path)


def test_a_corrupt_document_leaves_the_schedule_key_out(tmp_path):
    (tmp_path / SCHEDULE_FILE).write_text("{not json")

    assert dash_feed._schedule_edge(tmp_path) is None
    assert "schedule" not in _feed(tmp_path)


def test_snapshot_omits_the_key_when_given_no_schedule():
    assert "schedule" not in snapshot(_NOW, {}, {}, [], [], [], 0, [], 0, [])
