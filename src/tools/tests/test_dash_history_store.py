import sqlite3

from agent_tools.dash_history_store import read_history_rows, read_open_task_records


def _store(runs_dir):
    conn = sqlite3.connect(runs_dir / "cox.db")
    conn.execute("CREATE TABLE runs (run_id TEXT, launched_at TEXT, ended_at TEXT)")
    conn.execute("CREATE TABLE task_records (run_id TEXT, phase_id TEXT, task_id TEXT, record_json TEXT)")
    conn.execute("CREATE TABLE node_calls (run_id TEXT, task_id TEXT, role TEXT, cost_usd REAL, ts TEXT)")
    conn.execute("INSERT INTO runs VALUES ('r_live', '2026-10-04T09:00:00Z', NULL)")
    conn.execute("INSERT INTO runs VALUES ('r_done', '2026-10-04T08:00:00Z', '2026-10-04T08:30:00Z')")
    for run_id in ("r_live", "r_done"):
        conn.execute("INSERT INTO task_records VALUES (?, 'p1', 't1', '{}')", (run_id,))
        conn.execute("INSERT INTO node_calls VALUES (?, 't1', 'build', 0.5, '2026-10-04T08:10:00Z')", (run_id,))
    conn.commit()
    conn.close()


def test_only_the_ended_run_and_its_task_records_and_calls_come_back(tmp_path):
    _store(tmp_path)
    assert read_history_rows(tmp_path, "2026-10-04T00:00:00Z") == {
        "runs": [{"run_id": "r_done", "launched_at": "2026-10-04T08:00:00Z", "ended_at": "2026-10-04T08:30:00Z"}],
        "task_records": [{"run_id": "r_done", "phase_id": "p1", "task_id": "t1", "record_json": "{}"}],
        "node_calls": [{"run_id": "r_done", "task_id": "t1", "role": "build", "cost_usd": 0.5, "ts": "2026-10-04T08:10:00Z"}],
    }


def _store_with_stamps(runs_dir):
    conn = sqlite3.connect(runs_dir / "cox.db")
    conn.execute("CREATE TABLE runs (run_id TEXT, launched_at TEXT, ended_at TEXT)")
    conn.execute("CREATE TABLE task_records (run_id TEXT, phase_id TEXT, task_id TEXT, record_json TEXT, updated_at TEXT)")
    conn.execute("INSERT INTO runs VALUES ('r_live', '2026-10-04T09:00:00Z', NULL)")
    conn.execute("INSERT INTO runs VALUES ('r_done', '2026-10-04T08:00:00Z', '2026-10-04T08:30:00Z')")
    rows = [
        ("r_live", "t1", "2026-10-04T09:10:00Z"),
        ("r_live", "t0", "2026-10-03T09:10:00Z"),  # written before `since`
        ("r_done", "t1", "2026-10-04T08:20:00Z"),  # an ended run's record, which read_history_rows already reads
    ]
    conn.executemany("INSERT INTO task_records VALUES (?, 'p1', ?, '{}', ?)", rows)
    conn.commit()
    conn.close()


def test_open_task_records_are_the_unended_runs_records_written_since(tmp_path):
    _store_with_stamps(tmp_path)
    assert read_open_task_records(tmp_path, "2026-10-04T00:00:00Z") == [
        {"run_id": "r_live", "phase_id": "p1", "task_id": "t1", "record_json": "{}", "updated_at": "2026-10-04T09:10:00Z"},
    ]


def test_open_task_records_are_empty_with_no_store(tmp_path):
    assert read_open_task_records(tmp_path, "2026-10-04T00:00:00Z") == []
