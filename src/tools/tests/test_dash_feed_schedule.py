import json
from pathlib import Path

import pytest

from agent_tools import dash_feed
from agent_tools.chair_schedule import SCHEDULE_FILE

_NOW = "2026-11-06T00:00:00Z"
_SCHEDULE = json.loads((Path(__file__).parent / "fixtures" / "dash_feed_schedule_v1.json").read_text())["schedule"]


@pytest.fixture
def _bare_feed(monkeypatch):
    """`gather_feed` with every section reduced to nothing, so only the schedule wiring is under test."""
    sections = {"chair": [], "spend": {}, "hosts": [], "lanes": []}
    monkeypatch.setattr(dash_feed.console_screen, "gather", lambda *args, **kwargs: sections)
    monkeypatch.setattr(dash_feed, "_spend", lambda *args: {})
    monkeypatch.setattr(dash_feed, "_local_identity", lambda runs_dir, profile: ("host-b", 3))
    monkeypatch.setattr(dash_feed, "_local_login", lambda name: None)
    monkeypatch.setattr(dash_feed, "_queue", lambda runs_dir, live_runs=(): ([], 0))
    monkeypatch.setattr(dash_feed, "_courier_blob", lambda work_dir: "")
    monkeypatch.setattr(dash_feed.run_store, "initiative_short_ids", lambda runs_dir: {})


def _feed(runs_dir: Path) -> dict:
    return json.loads(json.dumps(dash_feed.gather_feed(runs_dir, runs_dir, _NOW)))


def test_a_schedule_document_is_passed_through_key_for_key(_bare_feed, tmp_path):
    (tmp_path / SCHEDULE_FILE).write_text(json.dumps({"schedule": _SCHEDULE}))

    assert _feed(tmp_path)["schedule"] == _SCHEDULE


def test_no_schedule_document_leaves_the_key_absent(_bare_feed, tmp_path):
    assert "schedule" not in _feed(tmp_path)


def test_a_corrupt_schedule_document_leaves_the_key_absent(_bare_feed, tmp_path):
    (tmp_path / SCHEDULE_FILE).write_text("{not json")

    assert "schedule" not in _feed(tmp_path)
