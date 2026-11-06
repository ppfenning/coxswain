import json
import sqlite3

import pytest

from agent_tools import run_store

NOW = "2026-09-25T12:00:00Z"
LIVE = "2026-09-25T12:02:00Z"


def nodes(*pairs):
    return json.dumps({"nodes": [{"id": f"n{i}", "status": status, "phase": phase} for i, (status, phase) in enumerate(pairs)]})


def store(runs_dir, progress, with_progress_column=True):
    conn = sqlite3.connect(runs_dir / "cox.db")
    conn.execute("CREATE TABLE runs (run_id TEXT, launched_at TEXT)")
    conn.execute("INSERT INTO runs VALUES ('x-3', '2026-09-25T09:00:00Z')")
    column = ", progress TEXT" if with_progress_column else ""
    conn.execute(f"CREATE TABLE leases (name TEXT PRIMARY KEY, holder TEXT, epoch INTEGER, expires_at TEXT, heartbeat_at TEXT{column})")
    if with_progress_column:
        conn.execute("INSERT INTO leases VALUES ('runs:x', 'x-3', 1, ?, '2026-09-25T11:59:50Z', ?)", (LIVE, progress))
    else:
        conn.execute("INSERT INTO leases VALUES ('runs:x', 'x-3', 1, ?, '2026-09-25T11:59:50Z')", (LIVE,))
    conn.commit()
    conn.close()


@pytest.mark.parametrize(
    ("progress", "phases"),
    [
        (nodes(("running", "p1"), ("running", "p2")), 2),
        (nodes(("running", "p1"), ("running", "p1")), 1),
        (nodes(("done", "p1"), ("failed", "p2")), 1),
        (nodes(("running", "p1"), ("done", "p2"), ("running", "p3")), 2),
        (nodes(("running", ""), ("running", None)), 1),
        (json.dumps({"nodes": [{"status": "running"}]}), 1),
        ("not json", 1),
        (json.dumps({"nodes": "x"}), 1),
        (json.dumps([1, 2]), 1),
        (None, 1),
    ],
)
def test_a_live_lane_reports_the_distinct_phases_of_its_running_nodes(tmp_path, progress, phases):
    store(tmp_path, progress)
    assert [lane.phases for lane in run_store.live_lanes(tmp_path, NOW)] == [phases]


def test_a_leases_table_without_a_progress_column_gives_one_phase(tmp_path):
    store(tmp_path, None, with_progress_column=False)
    assert [lane.phases for lane in run_store.live_lanes(tmp_path, NOW)] == [1]


def test_lane_phases_defaults_to_one_and_lost_lanes_keep_it(tmp_path):
    assert run_store.Lane("x-3", None, "a", "b").phases == 1
    store(tmp_path, nodes(("running", "p1"), ("running", "p2")))
    assert [lane.phases for lane in run_store.lost_lanes(tmp_path, "2026-09-25T13:00:00Z")] == [1]


def test_live_lanes_opens_the_store_once_for_the_lease_read_and_once_for_the_join(tmp_path, monkeypatch):
    store(tmp_path, nodes(("running", "p1")))
    opens = []
    real_open = run_store._open
    monkeypatch.setattr(run_store, "_open", lambda runs_dir: opens.append(runs_dir) or real_open(runs_dir))
    run_store.live_lanes(tmp_path, NOW)
    assert len(opens) == 2
