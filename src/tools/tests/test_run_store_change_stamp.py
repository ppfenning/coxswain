import sqlite3

import pytest

from agent_tools import run_store

ROWS = [{"relname": "runs", "changes": 5}, {"relname": "leases", "changes": 2}]
PG = "postgresql://h/cox"


class FakeConn:
    def __init__(self, rows=(), error=None):
        self.rows = list(rows)
        self.error = error
        self.sql = []
        self.closed = False

    def execute(self, sql):
        self.sql.append(sql)
        if self.error:
            raise self.error
        return self

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True


@pytest.fixture
def stamp_of(monkeypatch):
    def run(conn, url=PG):
        monkeypatch.setattr(run_store, "_store_url", lambda runs_dir: url)
        monkeypatch.setattr(run_store, "_open", lambda runs_dir: None if conn is None else (conn, "%s"))
        return run_store.store_change_stamp("/runs")

    return run


def test_same_rows_give_equal_stamps_in_any_order(stamp_of):
    first = stamp_of(FakeConn(ROWS))
    assert first is not None
    hash(first)
    assert stamp_of(FakeConn(ROWS[::-1])) == first


def test_changed_counter_gives_a_different_stamp(stamp_of):
    bumped = [{"relname": "runs", "changes": 6}, ROWS[1]]
    assert stamp_of(FakeConn(bumped)) != stamp_of(FakeConn(ROWS))


def test_tuple_rows_give_the_same_stamp_as_mapping_rows(stamp_of):
    conn = FakeConn([("runs", 5), ("leases", 2)])
    assert stamp_of(conn) == run_store._stamp_of_rows(ROWS)
    assert conn.closed


def test_sqlite_store_gives_none_without_opening(stamp_of):
    conn = FakeConn(ROWS)
    assert stamp_of(conn, url="sqlite:////s/cox.db") is None
    assert conn.sql == []


def test_failing_query_gives_none_and_closes(stamp_of):
    conn = FakeConn(error=sqlite3.DatabaseError("boom"))
    assert stamp_of(conn) is None
    assert conn.closed


def test_missing_psycopg_gives_none(monkeypatch):
    def no_psycopg(runs_dir):
        raise RuntimeError("psycopg is not installed")

    monkeypatch.setattr(run_store, "_store_url", lambda runs_dir: PG)
    monkeypatch.setattr(run_store, "_open", no_psycopg)
    assert run_store.store_change_stamp("/runs") is None


def test_unreadable_store_config_gives_none(monkeypatch):
    def bad_config(runs_dir):
        raise ValueError("malformed provider profile")

    monkeypatch.setattr(run_store, "_store_url", bad_config)
    assert run_store.store_change_stamp("/runs") is None


def test_connection_that_cannot_close_gives_none(stamp_of):
    class Unclosable(FakeConn):
        def close(self):
            raise OSError("socket gone")

    assert stamp_of(Unclosable(ROWS)) is None


def test_absent_store_gives_none(stamp_of):
    assert stamp_of(None) is None


def test_query_reads_write_counters_not_commits():
    sql = run_store._change_stamp_sql()
    assert all(word in sql for word in ("n_tup_ins", "n_tup_upd", "n_tup_del", "pg_stat_user_tables"))
    assert "xact_commit" not in sql
