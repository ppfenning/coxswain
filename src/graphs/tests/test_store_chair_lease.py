"""C3: a queue worker's kind lease on the store's clock, and the chair action write it fences in one transaction.

Run against the store fixture (the sqlite double, and Postgres where the suite has one). Expiry is set on the row
itself, since waiting out a real lease is a test of time, not of this code."""

from harness import store_chair_lease as kl

NAME = "chair:housekeeping"
_ACTION = {"ts": "2026-11-20T12:00:00Z", "epoch": 1, "kind": "housekeeping", "status": "done", "reason": "swept"}


def _expire(conn, name: str = NAME) -> None:
    p = conn.dialect.placeholder
    with conn.transaction():
        conn.execute(f"UPDATE leases SET expires_at = '1970-01-01T00:00:00Z' WHERE name = {p}", (name,))


def _rows(conn) -> list[tuple]:
    return conn.query_all("SELECT epoch, holder, kind, status FROM chair_actions ORDER BY ts")


def test_one_holder_takes_the_kind_lease_and_a_second_is_refused_until_it_lapses(store_conn):
    assert kl.acquire(store_conn, NAME, "a", 60) == 1
    assert kl.acquire(store_conn, NAME, "b", 60) is None
    assert kl.renew(store_conn, NAME, "a", 1, 60) is True
    _expire(store_conn)
    assert kl.renew(store_conn, NAME, "a", 1, 60) is False  # a lapsed lease is not revived
    assert kl.acquire(store_conn, NAME, "b", 60) == 2


def test_the_lease_times_are_the_stores_clock(store_conn):
    kl.acquire(store_conn, NAME, "a", 60)
    leases, now = kl.leases(store_conn, "chair:")
    assert set(leases) == {NAME}
    assert leases[NAME] > now  # expiry is the store's now plus the ttl, so it is after the store's now


def test_a_fenced_record_is_written_while_the_lease_holds_and_refused_once_another_worker_has_it(store_conn):
    assert kl.acquire(store_conn, NAME, "a", 60) == 1
    assert kl.holds(store_conn, NAME, "a", 1)
    assert kl.record_fenced(store_conn, NAME, "a", 1, "chair-a", _ACTION) is True
    _expire(store_conn)
    assert not kl.holds(store_conn, NAME, "a", 1)
    assert kl.acquire(store_conn, NAME, "b", 60) == 2
    later = {**_ACTION, "ts": "2026-11-20T12:01:00Z"}
    assert kl.record_fenced(store_conn, NAME, "a", 1, "chair-a", later) is False
    assert kl.record_fenced(store_conn, NAME, "b", 1, "chair-b", later) is False  # the holder, at a stale epoch
    assert _rows(store_conn) == [(1, "chair-a", "housekeeping", "done")]


def test_release_frees_the_kind_for_the_next_worker_at_a_new_epoch(store_conn):
    assert kl.acquire(store_conn, NAME, "a", 60) == 1
    assert kl.release(store_conn, NAME, "a", 1) is True
    assert kl.acquire(store_conn, NAME, "b", 60) == 2


def test_a_row_whose_lease_was_lost_after_its_effect_ran_is_kept_and_marked_late(store_conn):
    import json

    assert kl.acquire(store_conn, NAME, "a", 60) == 1
    _expire(store_conn)
    assert kl.record_fenced(store_conn, NAME, "a", 1, "chair-a", _ACTION) is False
    kl.record_late(store_conn, "chair-a", _ACTION)
    (row,) = store_conn.query_all("SELECT status, reason, action_json FROM chair_actions")
    body = row[2] if isinstance(row[2], dict) else json.loads(row[2])
    assert row[0] == "done" and body["late"] is True
    assert row[1] == "late, the kind lease was lost after the effect ran: swept"
