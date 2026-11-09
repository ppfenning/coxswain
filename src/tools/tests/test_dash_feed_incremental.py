import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_tools import dash_feed, dash_feed_cost
from agent_tools.console_screen import LaneRow

FIXTURE = Path(__file__).parent / "fixtures" / "dash_feed_cost_v1.json"
NOW = "2026-11-09T12:00:00Z"
LATER = "2026-11-09T12:00:30Z"
STORE_DRIVEN = [name for name in dash_feed_cost.SECTION_ORDER if name != "live"]
LANE = LaneRow("alpha-1", None, "", "p1", "build", 1, 2, 0.5, 0, 1)
CALL = {"ts": "2026-11-09T11:58:00Z", "node": "build", "attempt": 1, "turns": 2, "cost_usd": 0.5}
# The lease and the chair.json record disagree on the heartbeat on purpose: only the lease's counts on the store path.
LEASE = {
    "holder": "chair@host-a:1", "host": "host-a", "epoch": 3,
    "heartbeat_at": "2026-11-09T11:59:50Z", "expires_at": "2026-11-09T12:00:10Z",
    "status": json.dumps({"tick_at": "2026-11-09T11:59:41Z", "status": "ok"}),
}
HOUSEKEEPING = {"kind": "housekeeping", "status": "done", "ts": "2026-11-09T11:58:00Z"}
OLD_RUN = {
    "run": "old-1", "machine": "", "phase": "", "node": "", "attempt": 0, "turns": 0, "cost": 0.0, "verdict": "",
    "status": "landed", "ended_at": "2026-11-09T06:00:20Z", "calls": [],
}
WORLD: dict = {}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    runs, work, projects = tmp_path / "runs", tmp_path / "work", tmp_path / "projects"
    for directory in (runs, work, projects):
        directory.mkdir()
    (runs / "cox.db").write_bytes(b"store")
    (runs / "run-1.log").write_text("one\n")
    WORLD.clear()
    WORLD.update(lanes=[LANE], records=[])
    host = {"name": "host-a", "beat_at": "2026-11-09T11:59:00Z"}
    set_ = monkeypatch.setattr
    set_(dash_feed, "_PROJECTS_DIR", str(projects))
    set_(dash_feed, "_store_is_local_sqlite", lambda runs_dir: True)
    set_(dash_feed, "_local_login", lambda name: None)
    set_(dash_feed, "_local_identity", lambda runs_dir, profile: ("host-a", 2))
    set_(dash_feed.run_store, "initiative_short_ids", lambda runs_dir: {})
    set_(dash_feed.run_store, "read_queue", lambda runs_dir, **kw: [])
    set_(dash_feed, "_spend", lambda runs_dir, now: {"spend_age_s": 5, "five_hour_fraction": 0.1})
    set_(dash_feed, "_today_v1", lambda runs_dir, now: None)
    set_(dash_feed, "_activity", lambda *a, **k: {"chair_agents": [], "spend_split": None, "spend_sources": None})
    set_(dash_feed, "_live_edge", lambda *a, **k: None)
    set_(dash_feed, "_lane_targets", lambda *a, **k: [])
    set_(dash_feed, "_burn_meters", lambda *a, **k: {})
    set_(dash_feed, "_hard_stop_fraction", lambda runs_dir: 1.0)
    set_(dash_feed, "_budget_edge", lambda runs_dir, at: None)
    set_(dash_feed, "_store_rows", lambda runs_dir, at, spend: {})
    set_(dash_feed, "_finished_records", lambda runs_dir, ids: list(WORLD["records"]))
    set_(dash_feed, "_run_calls", lambda runs_dir, run: [CALL])
    set_(dash_feed, "_orchestration_edge", lambda *a: None)
    set_(dash_feed, "read_chair_rows", lambda runs_dir, at, offset: (dict(LEASE), [dict(HOUSEKEEPING)]))
    set_(dash_feed, "_chair_record", lambda runs_dir: {"heartbeat_at": "2026-11-09T11:59:57Z", "claude_session": "abc"})
    set_(dash_feed, "_ends_edge", lambda runs_dir, rows: {})
    set_(dash_feed, "_tasks_edge", lambda *a: {})
    set_(
        dash_feed, "_board_edge",
        lambda runs_dir, rows, states, calls, lanes, ids, local, at: (
            {"epics": []}, dash_feed._lanes_by_host(lanes, states, calls, at, local),
        ),
    )
    set_(dash_feed, "_queue", lambda runs_dir, live: ([], 0))
    set_(dash_feed, "read_review_prs", lambda runs_dir, state: [])
    set_(
        dash_feed.console_screen, "gather",
        lambda *a, **k: {"chair": [], "lanes": list(WORLD["lanes"]), "hosts": [host], "spend": {"spend_age_s": 5}},
    )
    return runs, work


def _without_cost(feed: dict) -> str:
    return json.dumps({k: v for k, v in feed.items() if k != "cost"}, sort_keys=True)


def _fresh(runs, work, now: str) -> str:
    return json.dumps(dash_feed.gather_feed(runs, work, now), sort_keys=True)


def _use_record_path(monkeypatch) -> None:
    """Make the store fetch return None, so the chair reads chair.json and the status line instead of the lease."""
    edge = {
        "status": ("11-09 06:59 EST", "chair 11-09 06:59 EST | ok"), "action": None, "action_rows": [],
        "last_housekeeping_at": "2026-11-09T11:58:00Z",
    }
    monkeypatch.setattr(dash_feed, "read_chair_rows", lambda runs_dir, at, offset: None)
    monkeypatch.setattr(dash_feed, "_lease", lambda runs_dir: {})
    monkeypatch.setattr(dash_feed, "_chair_edge", lambda runs_dir: edge)


def test_nothing_changed_reuses_every_store_driven_section(workspace):
    runs, work = workspace
    first, cache = dash_feed.gather_feed_cached(runs, work, NOW, None, dash_feed.EMPTY_CACHE)
    assert first["cost"]["recomputed"] == list(dash_feed_cost.SECTION_ORDER)
    again, _ = dash_feed.gather_feed_cached(runs, work, NOW, None, cache)
    assert again["cost"]["reused"] == list(dash_feed_cost.SECTION_ORDER)
    assert again["cost"]["recomputed"] == []
    assert _without_cost(again) == _fresh(runs, work, NOW)
    assert "cost" not in dash_feed.gather_feed(runs, work, NOW)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cache.memo = {}


def test_touching_a_run_record_recomputes_only_sections_that_read_records(workspace):
    runs, work = workspace
    _, cache = dash_feed.gather_feed_cached(runs, work, NOW, None, dash_feed.EMPTY_CACHE)
    (runs / "run-1.log").write_text("one\ntwo\n")
    feed, _ = dash_feed.gather_feed_cached(runs, work, NOW, None, cache)
    reading = [n for n in STORE_DRIVEN if "records" in dash_feed._SECTION_GROUPS[n]]
    assert feed["cost"]["recomputed"] == reading
    assert feed["cost"]["reused"] == ["epics", "live"]


@pytest.mark.parametrize(
    ("source", "beat_age", "tick_age"), [("store", 40, 49), ("record", 33, 90)],
)
def test_a_reused_feed_at_a_later_now_equals_a_fresh_one(workspace, monkeypatch, source, beat_age, tick_age):
    runs, work = workspace
    if source == "record":
        _use_record_path(monkeypatch)
    _, cache = dash_feed.gather_feed_cached(runs, work, NOW, None, dash_feed.EMPTY_CACHE)
    feed, _ = dash_feed.gather_feed_cached(runs, work, LATER, None, cache)
    assert feed["cost"]["reused"] == list(dash_feed_cost.SECTION_ORDER)
    assert _without_cost(feed) == _fresh(runs, work, LATER)
    assert (feed["chair"]["beat_age_s"], feed["chair"]["tick_age_s"]) == (beat_age, tick_age)
    assert feed["chair"]["housekeeping_age_s"] == 150
    assert feed["machines"][0]["beat_age_s"] == 90
    assert feed["machines"][0]["lanes"][0]["elapsed_s"] == 150


def test_a_lease_that_expires_between_refreshes_reads_stale(workspace):
    runs, work = workspace
    first, cache = dash_feed.gather_feed_cached(runs, work, NOW, None, dash_feed.EMPTY_CACHE)
    later, _ = dash_feed.gather_feed_cached(runs, work, LATER, None, cache)
    assert (first["chair"]["liveness"], later["chair"]["liveness"]) == ("live", "stale")


def test_a_run_aging_out_of_the_window_recomputes_runs(workspace):
    runs, work = workspace
    WORLD["records"] = [OLD_RUN]
    first, cache = dash_feed.gather_feed_cached(runs, work, NOW, None, dash_feed.EMPTY_CACHE)
    assert "old-1" in [row["run"] for row in first["runs"]]
    feed, _ = dash_feed.gather_feed_cached(runs, work, LATER, None, cache)
    assert "old-1" not in [row["run"] for row in feed["runs"]]
    assert feed["cost"]["recomputed"] == ["runs"]
    assert _without_cost(feed) == _fresh(runs, work, LATER)


def test_lanes_that_change_recompute_the_sections_built_from_them(workspace):
    runs, work = workspace
    _, cache = dash_feed.gather_feed_cached(runs, work, NOW, None, dash_feed.EMPTY_CACHE)
    WORLD["lanes"] = []
    feed, _ = dash_feed.gather_feed_cached(runs, work, LATER, None, cache)
    assert feed["cost"]["recomputed"] == ["machines", "runs", "queue", "history", "board"]
    assert _without_cost(feed) == _fresh(runs, work, LATER)


def test_section_key_floors_time_to_the_step():
    prints = {"store": (), "records": (), "lane_traces": (), "transcripts": ()}

    def key(minute: int, second: int) -> tuple:
        return dash_feed._section_key("chair", prints, datetime(2026, 11, 9, 12, minute, second, tzinfo=UTC), ())

    assert key(0, 1) == key(0, 59)
    assert key(0, 59) != key(1, 0)


def test_a_missing_fingerprint_never_matches():
    prints = {"store": None, "records": (), "lane_traces": (), "transcripts": ()}
    at = datetime(2026, 11, 9, 12, tzinfo=UTC)
    assert dash_feed._section_key("epics", prints, at, ()) != dash_feed._section_key("epics", prints, at, ())


def test_cost_block_matches_the_fixture_shape(workspace):
    runs, work = workspace
    feed, _ = dash_feed.gather_feed_cached(runs, work, NOW, None, dash_feed.EMPTY_CACHE)
    expected = json.loads(FIXTURE.read_text())["cost"]
    cost = feed["cost"]
    assert cost.keys() == expected.keys()
    assert all(isinstance(cost[key], type(expected[key])) for key in expected)
    assert set(cost["recomputed"]) | set(cost["reused"]) == set(dash_feed_cost.SECTION_ORDER)
