import sqlite3
from pathlib import Path

from agent_tools import chair_read_live
from agent_tools.chair_read_live import Lease, _phase_pidfiles, live_phases, read_live_phases

NOW = "2026-11-06T12:00:00Z"
FUTURE = Lease("run-1", "2026-11-06T13:00:00Z")
PAST = Lease("run-1", "2026-11-06T11:00:00Z")
P1 = ("init", "p1")


def leases_table(runs_dir: Path, *rows: tuple[str, str, str]) -> None:
    conn = sqlite3.connect(runs_dir / "cox.db")
    conn.execute("CREATE TABLE leases (name TEXT PRIMARY KEY, holder TEXT, epoch INTEGER, heartbeat_at TEXT, expires_at TEXT)")
    conn.executemany("INSERT INTO leases VALUES (?, ?, 1, '2026-11-06T11:59:00Z', ?)", rows)
    conn.commit()
    conn.close()


def test_a_held_unexpired_phase_lease_is_live():
    assert live_phases({P1: FUTURE}, {P1: False}, NOW) == [P1]


def test_an_expired_phase_lease_with_a_dead_pid_is_not_live():
    assert live_phases({P1: PAST}, {P1: False}, NOW) == []


def test_an_expired_phase_lease_with_a_live_pid_is_live():
    assert live_phases({P1: PAST}, {P1: True}, NOW) == [P1]


def test_live_phases_come_back_sorted():
    assert live_phases({("b", "p2"): FUTURE, ("a", "p9"): PAST}, {("a", "p1"): True, ("a", "p9"): True}, NOW) == [("a", "p1"), ("a", "p9"), ("b", "p2")]


def test_a_held_phase_lease_in_the_store_yields_the_pair(tmp_path: Path):
    leases_table(tmp_path, ("runs:init.p1", "init.p1", "2026-11-06T13:00:00Z"))
    assert read_live_phases(tmp_path, ["init"], NOW) == [P1]


def test_the_whole_initiative_lease_yields_no_phase_pair(tmp_path: Path):
    leases_table(tmp_path, ("runs:init", "init-1", "2026-11-06T13:00:00Z"))
    assert read_live_phases(tmp_path, ["init"], NOW) == []


def test_another_initiatives_phase_lease_is_not_matched(tmp_path: Path):
    leases_table(tmp_path, ("runs:init2.p1", "init2.p1", "2026-11-06T13:00:00Z"))
    assert read_live_phases(tmp_path, ["init"], NOW) == []


def test_the_pidfile_glob_for_init_skips_init2(tmp_path: Path):
    for name in ("init.p1.pid", "init.p2-3.pid", "init2.p1.pid", "init.pid", "init.p1.extra.pid"):
        (tmp_path / name).write_text("1")
    assert [(phase, p.name) for phase, p in _phase_pidfiles(tmp_path, "init")] == [("p1", "init.p1.pid"), ("p2", "init.p2-3.pid")]


def test_a_live_pidfile_yields_the_pair_and_a_dead_one_does_not(tmp_path: Path, monkeypatch):
    (tmp_path / "init.p1-2.pid").write_text("11")
    (tmp_path / "init.p2.pid").write_text("22")
    monkeypatch.setattr(chair_read_live, "_run_live", lambda pidfile, now: pidfile.name == "init.p1-2.pid")
    assert read_live_phases(tmp_path, ["init"], NOW) == [P1]


def test_the_edge_reads_no_live_phase_from_an_empty_runs_dir(tmp_path: Path):
    assert read_live_phases(tmp_path, ["init"], NOW) == []
