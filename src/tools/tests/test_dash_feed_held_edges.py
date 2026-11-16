"""The console gather, the review PRs and the queue rows and states are held under keys, so an idle refresh skips them."""

import json
from collections import Counter

import pytest

from agent_tools import dash_feed
from agent_tools.console_screen import LaneRow

NOW = "2026-11-09T12:00:00Z"
LATER = "2026-11-09T12:00:30Z"
NEXT_MINUTE = "2026-11-09T12:01:05Z"
LANE = LaneRow("alpha-1", None, "", "p1", "build", 1, 2, 0.5, 0, 1)
CALL = {"ts": "2026-11-09T11:58:00Z", "node": "build", "attempt": 1, "turns": 2, "cost_usd": 0.5}
HOST = {"name": "host-a", "beat_at": "2026-11-09T11:59:00Z"}
ROW = {"kind": "task", "initiative": "alpha", "phase": "p1", "task_id": "t1", "state": "building", "needs": []}


@pytest.fixture
def counted(tmp_path, monkeypatch):
    """A workspace on a Postgres-style store whose change stamp the test moves, with the three readers counted."""
    runs, work, projects = tmp_path / "runs", tmp_path / "work", tmp_path / "projects"
    for directory in (runs, work, projects):
        directory.mkdir()
    calls: Counter = Counter()
    world = {"stamp": (("runs", 1),), "login": None, "spend": {"spend_age_s": 5}, "chair": None}

    def count(name, value):
        def reader(*args, **kwargs):
            calls[name] += 1
            return value() if callable(value) else value
        return reader

    set_ = monkeypatch.setattr
    set_(dash_feed, "_PROJECTS_DIR", str(projects))
    set_(dash_feed, "_store_is_local_sqlite", lambda runs_dir: False)
    set_(dash_feed.run_store, "store_change_stamp", lambda runs_dir: world["stamp"])
    set_(dash_feed, "_local_login", lambda name: world["login"])
    set_(dash_feed, "_local_identity", lambda runs_dir, profile: ("host-b", 2))
    set_(dash_feed.run_store, "initiative_short_ids", lambda runs_dir: {})
    set_(dash_feed.run_store, "read_queue", lambda runs_dir, **kw: [])
    set_(dash_feed, "_spend", lambda runs_dir, now: dict(world["spend"]))
    set_(dash_feed, "_today_v1", lambda runs_dir, now: None)
    set_(dash_feed, "_activity", lambda *a, **k: {"chair_agents": [], "spend_split": None, "spend_sources": None})
    set_(dash_feed, "_live_edge", lambda *a, **k: None)
    set_(dash_feed, "_hard_stop_fraction", lambda runs_dir: 1.0)
    set_(dash_feed, "_budget_edge", lambda runs_dir, at: None)
    set_(dash_feed, "_store_rows", lambda runs_dir, at, spend: {})
    set_(dash_feed, "_finished_records", lambda runs_dir, ids: [])
    set_(dash_feed, "_run_calls", lambda runs_dir, run: [CALL])
    set_(dash_feed, "_orchestration_edge", lambda *a: None)
    set_(dash_feed, "read_chair_rows", lambda runs_dir, at, offset: None)
    set_(dash_feed, "_lease", lambda runs_dir: {})
    set_(dash_feed, "_chair_record", lambda runs_dir: {})
    set_(dash_feed, "_chair_edge", lambda runs_dir: {"status": (None, None), "action": None, "action_rows": [], "last_housekeeping_at": None})
    set_(dash_feed, "_ends_edge", lambda runs_dir, rows: {})
    set_(dash_feed, "_tasks_edge", lambda *a: {})
    set_(dash_feed, "_queue", lambda runs_dir, live: ([], 0))
    set_(dash_feed, "_task_records", count("task_records", []))
    set_(dash_feed, "_queue_rows_edge", count("queue_rows", lambda: [dict(ROW)]))
    set_(dash_feed, "read_review_prs", count("review_prs", []))

    def gather(runs_dir, work_dir, now, local_name, local_capacity, spend, local_versions=None):
        calls["console"] += 1
        hosts = dash_feed.console_screen.with_local_host([dict(HOST)], local_name, local_capacity, local_versions)
        chair = dash_feed.console_screen.runs_top_screen.chair_now(runs_dir)
        return {"chair": [chair] if chair else [], "lanes": [LANE], "hosts": hosts, "spend": spend}

    # As the real gather does, its chair row is `runs_top_screen.chair_now`'s answer.
    set_(dash_feed.console_screen.runs_top_screen, "chair_now", lambda runs_dir: world["chair"])
    set_(dash_feed.console_screen, "gather", gather)
    return runs, work, calls, world


def _without_cost(feed: dict) -> str:
    return json.dumps({k: v for k, v in feed.items() if k != "cost"}, sort_keys=True)


def _fresh(runs, work, now: str) -> str:
    return json.dumps(dash_feed.gather_feed(runs, work, now), sort_keys=True)


READERS = ("console", "review_prs", "queue_rows", "task_records")


def test_a_second_refresh_with_no_input_change_calls_none_of_the_readers(counted):
    runs, work, calls, _ = counted
    _, cache = dash_feed.gather_feed_cached(runs, work, NOW, None, dash_feed.EMPTY_CACHE)
    assert all(calls[name] == 1 for name in READERS)
    dash_feed.gather_feed_cached(runs, work, LATER, None, cache)
    assert [calls[name] for name in READERS] == [1, 1, 1, 1]


def test_a_store_change_recomputes_them(counted):
    runs, work, calls, world = counted
    _, cache = dash_feed.gather_feed_cached(runs, work, NOW, None, dash_feed.EMPTY_CACHE)
    world["stamp"] = (("runs", 2),)
    dash_feed.gather_feed_cached(runs, work, LATER, None, cache)
    assert [calls[name] for name in READERS] == [2, 2, 2, 2]


def test_a_new_minute_regathers_the_console_but_not_the_store_reads(counted):
    # The console gather reads lease expiry and chair liveness against `now`, so it is held for one minute at most.
    runs, work, calls, _ = counted
    _, cache = dash_feed.gather_feed_cached(runs, work, NOW, None, dash_feed.EMPTY_CACHE)
    dash_feed.gather_feed_cached(runs, work, NEXT_MINUTE, None, cache)
    assert [calls[name] for name in READERS] == [2, 1, 1, 1]


def test_a_reused_feed_at_a_later_now_equals_a_fresh_one(counted):
    runs, work, calls, world = counted
    world["login"] = True
    _, cache = dash_feed.gather_feed_cached(runs, work, NOW, None, dash_feed.EMPTY_CACHE)
    world["spend"] = {"spend_age_s": 35}
    feed, _ = dash_feed.gather_feed_cached(runs, work, LATER, None, cache)
    assert calls["console"] == 1
    assert _without_cost(feed) == _fresh(runs, work, LATER)
    local = next(machine for machine in feed["machines"] if machine["name"] == "host-b")
    assert (local["login_ok"], local["login_checked_at"]) == (True, LATER)
    assert feed["spend"]["spend_age_s"] == 35


def test_a_changed_login_answer_regathers_the_console(counted):
    runs, work, calls, world = counted
    _, cache = dash_feed.gather_feed_cached(runs, work, NOW, None, dash_feed.EMPTY_CACHE)
    world["login"] = False
    dash_feed.gather_feed_cached(runs, work, LATER, None, cache)
    assert calls["console"] == 2


def test_none_of_the_internal_holds_shows_in_the_cost_block(counted):
    runs, work, _, _ = counted
    feed, _ = dash_feed.gather_feed_cached(runs, work, NOW, None, dash_feed.EMPTY_CACHE)
    listed = {*feed["cost"]["recomputed"], *feed["cost"]["reused"]}
    assert listed.isdisjoint({"console", "review_prs", "queue_rows", "queue_states"})


def test_a_reused_console_reads_the_chair_afresh(counted):
    # The chair row (chair.json, its pid and heartbeat) moves with no store write, so a held gather must not carry it.
    runs, work, calls, world = counted
    world["chair"] = {"holder": "chair-a", "state": "live", "minutes_ago": 0}
    _, cache = dash_feed.gather_feed_cached(runs, work, NOW, None, dash_feed.EMPTY_CACHE)
    world["chair"] = {"holder": "chair-b", "state": "crashed", "minutes_ago": 0}
    feed, _ = dash_feed.gather_feed_cached(runs, work, LATER, None, cache)
    assert calls["console"] == 1
    assert feed["chair"]["holder"] == "chair-b"
    assert _without_cost(feed) == _fresh(runs, work, LATER)
