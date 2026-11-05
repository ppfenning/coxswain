import sqlite3
from datetime import UTC, datetime

from agent_tools import chair
from agent_tools.chair import lease_holder
from agent_tools.chair_facts import lease_facts
from agent_tools.chair_read_lease import read_lease

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
HOLDER = lease_holder("s", 7, "h")
RECORD = {"session": "s", "pid": 7, "host": "h", "heartbeat_at": "2026-09-26T11:58:00+00:00", "until": "2026-09-26T18:00:00+00:00"}


def _store(runs_dir, expires_at="2026-09-26T12:05:00Z"):
    conn = sqlite3.connect(runs_dir / "cox.db")
    conn.execute("CREATE TABLE leases (name TEXT PRIMARY KEY, holder TEXT, epoch INTEGER, heartbeat_at TEXT, expires_at TEXT)")
    conn.execute("INSERT INTO leases VALUES ('chair', ?, 4, '2026-09-26T11:59:00Z', ?)", (HOLDER, expires_at))
    conn.commit()
    conn.close()


def _lent(runs_dir, record, expires_at="2026-09-26T12:05:00Z"):
    _store(runs_dir, expires_at)
    chair.write(runs_dir, record)
    return read_lease(runs_dir, NOW, pid_alive=lambda pid: True, host="h").get("lent", False)


def test_a_live_matching_holder_with_routine_lent_true_reads_lent(tmp_path):
    assert _lent(tmp_path, {**RECORD, "routine_lent": True}) is True


def test_an_absent_or_false_routine_lent_reads_not_lent(tmp_path):
    assert _lent(tmp_path, RECORD) is False
    (tmp_path / "chair.json").unlink()
    (tmp_path / "cox.db").unlink()
    assert _lent(tmp_path, {**RECORD, "routine_lent": False}) is False


def test_a_record_naming_another_holder_reads_not_lent(tmp_path):
    assert _lent(tmp_path, {**RECORD, "session": "zed", "routine_lent": True}) is False


def test_a_released_lease_reads_not_lent(tmp_path):
    assert _lent(tmp_path, {**RECORD, "routine_lent": True}, expires_at="1970-01-01T00:00:00Z") is False


def test_a_record_with_no_until_still_reads_lent(tmp_path):
    record = {key: value for key, value in RECORD.items() if key != "until"}
    assert _lent(tmp_path, {**record, "routine_lent": True}) is True


def test_a_non_boolean_routine_lent_reads_not_lent(tmp_path):
    assert _lent(tmp_path, {**RECORD, "routine_lent": "true"}) is False


def test_lease_facts_copies_lent_as_a_bool_and_defaults_to_false():
    row = {"holder": HOLDER, "host": "h", "epoch": 4, "released": False, "stale": False}
    assert lease_facts({**row, "lent": True}, "other", 1, "h")["lent"] is True
    assert lease_facts(row, "other", 1, "h")["lent"] is False
