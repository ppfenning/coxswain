import sqlite3
from datetime import UTC, datetime

from agent_tools import chair
from agent_tools.chair import lease_holder
from agent_tools.chair_facts import lease_facts
from agent_tools.chair_plan import _lease_gate
from agent_tools.chair_read_lease import ABSENT, lease_record, read_lease

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
HOLDER = lease_holder("s", 7, "h")


def test_a_held_lease_inside_its_expiry_is_neither_stale_nor_released():
    row = {"holder": HOLDER, "epoch": 3, "expires_at": "2026-09-26T12:05:00Z"}
    assert lease_record(row, NOW) == {"holder": HOLDER, "host": "h", "epoch": 3, "released": False, "stale": False}


def test_an_expired_lease_is_stale():
    assert lease_record({"holder": HOLDER, "epoch": 3, "expires_at": "2026-09-26T11:59:00Z"}, NOW)["stale"] is True


def test_a_released_lease_is_released():
    assert lease_record({"holder": HOLDER, "epoch": 3, "expires_at": "1970-01-01T00:00:00Z"}, NOW)["released"] is True
    assert lease_record({"holder": None, "epoch": None, "expires_at": None}, NOW)["released"] is True


def test_no_lease_plans_a_take():
    assert lease_record(None, NOW) == ABSENT
    assert _lease_gate(lease_facts(lease_record(None, NOW), "s", 7, "h")) == [{"kind": "take_lease"}]


def _store(runs_dir, *rows):
    conn = sqlite3.connect(runs_dir / "cox.db")
    conn.execute("CREATE TABLE leases (name TEXT PRIMARY KEY, holder TEXT, epoch INTEGER, heartbeat_at TEXT, expires_at TEXT)")
    conn.executemany("INSERT INTO leases VALUES (?, ?, ?, '2026-09-26T11:59:00Z', ?)", rows)
    conn.commit()
    conn.close()


def test_the_edge_reads_the_chair_row_and_ignores_others(tmp_path):
    _store(tmp_path, ("runs:x", "x-1", 1, "2026-09-26T13:00:00Z"), ("chair", HOLDER, 4, "2026-09-26T12:05:00Z"))
    assert read_lease(tmp_path, NOW) == {"holder": HOLDER, "host": "h", "epoch": 4, "released": False, "stale": False}


def test_a_store_whose_leases_table_has_no_epoch_column_still_reads_the_held_lease_with_epoch_zero(tmp_path):
    conn = sqlite3.connect(tmp_path / "cox.db")
    conn.execute("CREATE TABLE leases (name TEXT PRIMARY KEY, holder TEXT, heartbeat_at TEXT, expires_at TEXT)")
    conn.execute("INSERT INTO leases VALUES ('chair', ?, '2026-09-26T11:59:00Z', '2026-09-26T12:05:00Z')", (HOLDER,))
    conn.commit()
    conn.close()
    assert read_lease(tmp_path, NOW) == {"holder": HOLDER, "host": "h", "epoch": 0, "released": False, "stale": False}


def test_the_edge_reads_absent_with_no_store_or_no_chair_row(tmp_path):
    assert read_lease(tmp_path, NOW) == ABSENT
    _store(tmp_path, ("runs:x", "x-1", 1, "2026-09-26T13:00:00Z"))
    assert read_lease(tmp_path, NOW) == ABSENT


def test_the_edge_adds_until_and_expired_from_the_holders_chair_record(tmp_path):
    _store(tmp_path, ("chair", HOLDER, 4, "2026-09-26T12:05:00Z"))
    chair.write(tmp_path, {"session": "s", "pid": 7, "host": "h", "until": "2026-09-26T11:00:00+00:00"})
    lease = read_lease(tmp_path, NOW)
    assert (lease["until"], lease["expired"]) == ("2026-09-26T11:00:00+00:00", True)
    assert lease_facts(lease, "other", 1, "h")["expired"] is True


def test_the_edge_ignores_the_until_of_another_holders_chair_record(tmp_path):
    _store(tmp_path, ("chair", HOLDER, 4, "2026-09-26T12:05:00Z"))
    chair.write(tmp_path, {"session": "zed", "pid": 7, "host": "h", "until": "2026-09-26T11:00:00+00:00"})
    assert "expired" not in read_lease(tmp_path, NOW)


def _held(runs_dir, *, session="s", pid=7, host="h", until="2026-09-26T18:00:00+00:00", expires_at="2026-09-26T11:00:00Z"):
    """A store row past its expiry and the same holder's chair.json, last heartbeat an hour before NOW."""
    _store(runs_dir, ("chair", lease_holder(session, pid, host), 4, expires_at))
    record = {"session": session, "pid": pid, "host": host, "heartbeat_at": "2026-09-26T11:00:00+00:00"}
    chair.write(runs_dir, {**record, **({"until": until} if until else {})})


def _alive(pid):
    return True


def _dead(pid):
    return False


def _gate(lease):
    return _lease_gate(lease_facts(lease, "other", 1, "h"))


def test_a_live_holder_record_clears_a_stale_store_expiry_and_plans_standby(tmp_path):
    _held(tmp_path)
    lease = read_lease(tmp_path, NOW, pid_alive=_alive, host="h")
    assert (lease["stale"], lease["expired"], lease["until"]) == (False, False, "2026-09-26T18:00:00+00:00")
    assert _gate(lease) == [{"kind": "standby", "holder": HOLDER, "host": "h", "until": "2026-09-26T18:00:00+00:00"}]


def test_a_dead_pid_leaves_the_store_expiry_stale(tmp_path):
    _held(tmp_path)
    lease = read_lease(tmp_path, NOW, pid_alive=_dead, host="h")
    assert lease["stale"] is True
    assert _gate(lease) == [{"kind": "take_lease"}]


def test_a_passed_until_leaves_the_store_expiry_stale(tmp_path):
    _held(tmp_path, until="2026-09-26T11:30:00+00:00")
    lease = read_lease(tmp_path, NOW, pid_alive=_alive, host="h")
    assert (lease["stale"], lease["expired"]) == (True, True)
    assert _gate(lease)[0]["kind"] == "take_lease"


def test_a_silent_record_from_another_host_leaves_the_store_expiry_stale(tmp_path):
    _held(tmp_path, host="elsewhere")
    lease = read_lease(tmp_path, NOW, pid_alive=_alive, host="h")
    assert lease["stale"] is True
    assert _gate(lease) == [{"kind": "take_lease"}]


def test_a_released_lease_stays_released_beside_a_live_record(tmp_path):
    _held(tmp_path, expires_at="1970-01-01T00:00:00Z")
    lease = read_lease(tmp_path, NOW, pid_alive=_alive, host="h")
    assert (lease["released"], lease["stale"]) == (True, True)
    assert _gate(lease) == [{"kind": "take_lease"}]


def test_the_store_expiry_alone_decides_with_no_record_or_no_until(tmp_path):
    _held(tmp_path, until="")
    assert read_lease(tmp_path, NOW, pid_alive=_alive, host="h")["stale"] is True
    chair.write(tmp_path, None)
    assert read_lease(tmp_path, NOW, pid_alive=_alive, host="h")["stale"] is True


def test_another_holders_live_record_does_not_clear_the_store_expiry(tmp_path):
    _held(tmp_path)
    chair.write(tmp_path, {"session": "zed", "pid": 7, "host": "h", "until": "2026-09-26T18:00:00+00:00", "heartbeat_at": "2026-09-26T11:59:00+00:00"})
    assert read_lease(tmp_path, NOW, pid_alive=_alive, host="h")["stale"] is True


def _session_record(runs_dir, **extra):
    chair.write(runs_dir, {"session": "s", "pid": 7, "host": "h", "heartbeat_at": "2026-09-26T11:00:00+00:00", **extra})


def test_a_plain_session_record_sets_session_held(tmp_path):
    _held(tmp_path, until="")
    lease = read_lease(tmp_path, NOW, pid_alive=_alive, host="h")
    assert lease["session_held"] is True
    assert lease_facts(lease, "other", 1, "h")["session_held"] is True


def test_an_until_record_leaves_session_held_unset(tmp_path):
    _held(tmp_path)
    assert read_lease(tmp_path, NOW, pid_alive=_alive, host="h").get("session_held", False) is False


def test_a_taken_over_from_record_leaves_session_held_unset(tmp_path):
    _held(tmp_path, until="")
    _session_record(tmp_path, taken_over_from="old")
    assert read_lease(tmp_path, NOW, pid_alive=_alive, host="h").get("session_held", False) is False


def test_a_lent_record_leaves_session_held_unset(tmp_path):
    _held(tmp_path, until="")
    _session_record(tmp_path, routine_lent=True)
    lease = read_lease(tmp_path, NOW, pid_alive=_alive, host="h")
    assert (lease["lent"], lease.get("session_held", False)) == (True, False)


def test_a_released_lease_never_sets_session_held(tmp_path):
    _held(tmp_path, until="", expires_at="1970-01-01T00:00:00Z")
    lease = read_lease(tmp_path, NOW, pid_alive=_alive, host="h")
    assert (lease["released"], lease.get("session_held", False)) == (True, False)


def test_a_thief_holding_the_lease_with_a_live_record_plans_standby_not_take(tmp_path):
    _held(tmp_path, session="thief", pid=9)
    thief = lease_holder("thief", 9, "h")
    lease = read_lease(tmp_path, NOW, pid_alive=_alive, host="h")
    assert lease["stale"] is False
    assert _gate(lease) == [{"kind": "standby", "holder": thief, "host": "h", "until": "2026-09-26T18:00:00+00:00"}]
