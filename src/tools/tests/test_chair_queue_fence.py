"""C3: a queue worker that loses its kind lease in the middle of a pass performs nothing after the loss.

The lease lives in a real store (the sqlite double, opened by the harness's own migrations), and the fence asks it
before every action. The first action's effect is another worker taking the lease over; the second and third are
then fenced and never run. Nothing is stubbed: `perform_one` is a list that records what ran."""

from pathlib import Path

from agent_tools import run_store
from agent_tools.chair_queue import bounded, perform_fenced
from harness import store_chair_lease as kl
from harness.store_migrate import open_store

NAME = "chair:housekeeping"


def _store(tmp_path: Path):
    (tmp_path / "runs").mkdir()
    return open_store(run_store.store_url_for(tmp_path / "runs"), "2026-11-20T00:00:00Z")


def test_a_lease_lost_mid_pass_fences_every_later_action_and_none_of_them_runs(tmp_path: Path):
    conn = _store(tmp_path)
    try:
        epoch = kl.acquire(conn, NAME, "worker-a", 60)
        assert epoch == 1
        ran: list[str] = []

        def perform_one(action: dict) -> list:
            ran.append(action["kind"])
            if len(ran) == 1:  # while the first action runs, another worker takes the kind over
                with conn.transaction():
                    conn.execute("UPDATE leases SET expires_at = '1970-01-01T00:00:00Z' WHERE name = ?", (NAME,))
                assert kl.acquire(conn, NAME, "worker-b", 60) == 2
            return [{"action": action, "status": "done", "reason": ""}]

        actions = [{"kind": "stale_to_draft", "epoch": 1}, {"kind": "housekeeping", "epoch": 1}, {"kind": "tune_lanes", "epoch": 1}]
        results = perform_fenced(actions, lambda: kl.holds(conn, NAME, "worker-a", epoch), perform_one)  # type: ignore[arg-type]
        assert ran == ["stale_to_draft"]
        assert [r["status"] for r in results] == ["done", "fenced", "fenced"]
        assert not kl.record_fenced(conn, NAME, "worker-a", epoch, "chair-a", {**actions[1], "ts": "2026-11-20T00:00:01Z", "status": "done"})
        assert conn.query_all("SELECT kind FROM chair_actions") == []
    finally:
        conn.close()


def test_a_lease_call_that_hangs_or_raises_reads_as_not_held():
    import threading

    hang = threading.Event()
    assert bounded(lambda: hang.wait(5) or True, 0.05, False) is False  # still waiting at the bound: not held
    hang.set()
    assert bounded(lambda: 1 // 0 == 0, 1.0, False) is False
    assert bounded(lambda: 7, 1.0, None) == 7


def test_the_tick_reads_the_worker_leases_on_the_stores_clock_and_an_unreadable_store_as_unknown(tmp_path: Path):
    from agent_tools import chair_kinds, chair_read_queues

    conn = _store(tmp_path)
    try:
        kl.acquire(conn, NAME, "worker-a", 60)
    finally:
        conn.close()
    leases, now = chair_read_queues.read_queue_leases(tmp_path / "runs")
    assert set(leases or {}) == {"housekeeping"} and chair_kinds.live_kinds(leases or {}, now) == {"housekeeping"}
    assert chair_read_queues.read_queue_leases(tmp_path / "absent") == ({}, "")  # no store yet: no lease
    broken = run_store.sqlite_path(run_store.store_url_for(tmp_path / "broken" / "runs"))
    broken.parent.mkdir(parents=True)
    broken.write_text("not a database", encoding="utf-8")
    assert chair_read_queues.read_queue_leases(tmp_path / "broken" / "runs") == (None, "")
