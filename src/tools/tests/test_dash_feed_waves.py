import json
from pathlib import Path

import pytest

from agent_tools import dash_feed

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "dash_feed_waves_v1.json").read_text(encoding="utf-8"))
INITIATIVE = "plan-an-initiative-as-waves-of-phases"


def _task(phase, task_id, state, needs=(), **extra):
    return {
        "kind": "task", "initiative": INITIATIVE, "phase": phase, "task_id": task_id, "state": state,
        "needs": list(needs), "extra": extra,
        **({"reason": extra.pop("reason")} if "reason" in extra else {}),
    }


ROWS = [
    _task("p1-schema", "schema-draft", "in_progress", priority=1),
    _task("p1-schema", "schema-docs", "ready"),
    _task("p1-store", "store-table", "done"),
    _task("p1-store", "store-index", "done", land_at="phase"),
    _task("p2-feed", "feed-emit", "ready", ["schema-draft"]),
    _task("p2-store", "store-migrate", "quarantined", ["schema-draft"], reason="review rejected twice"),
    _task("p2-store", "store-backfill", "ready"),
    _task("p3-join", "join-report", "ready", ["store-migrate"]),
]
LANES = {(INITIATIVE, "p1-schema"): "wave-run-1"}
# `_task_state` reads every idle task of a live lane's phase as `building` and a ready task behind a quarantined need
# as `blocked`, so the fixture's `ready` tasks beside them cannot come from rows. The task states are pinned here.
FIXTURE_STATES = [
    (phase["id"], task["id"], task["state"]) for phase in FIXTURE["queue"][0]["phases"] for task in phase["tasks"]
]


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(dash_feed.run_store, "read_queue", lambda runs_dir: ROWS)
    monkeypatch.setattr(dash_feed, "_task_records", lambda runs_dir: [])


def test_the_row_equals_the_fixture_exactly(store, monkeypatch, tmp_path):
    monkeypatch.setattr(dash_feed, "_task_states", lambda rows, initiative, live, stranded: FIXTURE_STATES)
    rows, total = dash_feed._queue(tmp_path, LANES)
    row = dash_feed._queue_v1(rows[0], {INITIATIVE: "I500"}, {INITIATIVE: "example-repo"})
    assert (row, total) == (FIXTURE["queue"][0], FIXTURE["queue_total"])


def test_plain_pairs_leave_the_rows_as_they_were(store, tmp_path):
    row = dash_feed._queue(tmp_path, {(INITIATIVE, "p1-schema")})[0][0]
    assert "waves" not in row and "widest_wave" not in row
    assert all(set(phase) == {"id", "tasks"} for phase in row["phases"])


def test_the_wave_keys_ride_on_the_real_task_states(store, tmp_path):
    row = dash_feed._queue(tmp_path, LANES)[0][0]
    assert [(p["id"], p["wave"], p["lane"], p["status"]) for p in row["phases"]] == [
        ("p1-schema", 1, "wave-run-1", "running"),
        ("p1-store", 1, None, "complete"),
        ("p2-feed", 2, None, "waiting"),
        ("p2-store", 2, None, "blocked"),
        ("p3-join", 3, None, "blocked"),
    ]
    assert row["phases"][0]["tasks"] == [{"id": "schema-draft", "state": "building"}, {"id": "schema-docs", "state": "building"}]


def test_live_lanes_carry_the_run_id_and_keep_the_pairs():
    lanes = [
        dash_feed.console_screen.LaneRow("beta-2", None, "", "p1", "build", 1, 3, 0.5, 0, 2),
        dash_feed.console_screen.LaneRow("beta-10", "host-a", "", "p1", "build", 1, 3, 0.5, 0, 2),
        dash_feed.console_screen.LaneRow("alpha-1", None, "", "", "starting", 0, 0, 0.0, 0, 1),
    ]
    assert dash_feed._live_lanes(lanes) == {("beta", "p1"): "beta-10", ("alpha", ""): "alpha-1"}
    assert set(dash_feed._live_lanes(lanes)) == dash_feed._live_phases(lanes)


def test_a_blank_phase_lane_runs_every_phase():
    row = {"initiative": "a", "phases": [{"id": "p1", "tasks": []}]}
    rows = [{"kind": "task", "initiative": "a", "phase": "p1", "task_id": "t", "state": "ready", "needs": []}]
    assert dash_feed._waved(row, rows, {("a", ""): "a-1"})["phases"][0]["lane"] == "a-1"


def test_a_phase_cycle_has_no_wave():
    row = {"initiative": "a", "phases": [{"id": "p1", "tasks": []}, {"id": "p2", "tasks": []}]}
    rows = [
        {"kind": "task", "initiative": "a", "phase": "p1", "task_id": "t1", "state": "ready", "needs": ["t2"]},
        {"kind": "task", "initiative": "a", "phase": "p2", "task_id": "t2", "state": "ready", "needs": ["t1"]},
    ]
    waved = dash_feed._waved(row, rows, {})
    assert (waved["waves"], waved["widest_wave"], [p["wave"] for p in waved["phases"]]) == ([], 0, [None, None])
    assert [p["status"] for p in waved["phases"]] == ["waiting", "waiting"]


@pytest.mark.parametrize(
    ("tasks", "lane", "parents", "parent_status", "expected"),
    [
        ([{"task_id": "a", "state": "quarantined"}], "r-1", ["p0"], {"p0": "blocked"}, ("running", None)),
        ([{"task_id": "a", "state": "quarantined"}], None, [], {}, ("blocked", "task a is quarantined")),
        ([{"task_id": "a", "state": "done"}, {"task_id": "b", "state": "dropped"}], None, ["p0"], {}, ("complete", None)),
        ([{"task_id": "a", "state": "approved", "extra": {"merged": True}}], None, [], {}, ("merged", None)),
        ([{"task_id": "a", "state": "approved"}], None, [], {}, ("waiting", None)),
        ([{"task_id": "a", "state": "ready"}], None, ["p0", "p1"], {"p0": "complete", "p1": "running"}, ("waiting", "waiting on p1")),
        ([{"task_id": "a", "state": "ready"}], None, ["p0"], {"p0": "blocked"}, ("blocked", "parent p0 is blocked")),
        ([], None, [], {}, ("waiting", None)),
    ],
)
def test_phase_status_by_rule(tasks, lane, parents, parent_status, expected):
    assert dash_feed._phase_status(tasks, lane, parents, parent_status) == expected
