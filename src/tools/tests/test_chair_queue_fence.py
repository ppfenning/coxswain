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


def test_a_launch_pass_is_one_perform_and_a_lease_lost_mid_pass_fences_the_launches_after_it(tmp_path: Path):
    """C5: the launch worker performs its pass as one batch (`perform_batched`), so the rules across its actions hold,
    and the epoch the perform reads before each action asks the store: once another worker has taken `chair:launch`,
    every later launch is fenced and its command never runs. The perform is the tick's own (`chair_exec.perform`)."""
    from agent_tools import chair_kinds
    from agent_tools.chair_exec import perform
    from agent_tools.chair_queue import perform_batched
    from tests.test_chair_queue_launch import _launch_deps

    conn = _store(tmp_path)
    name = chair_kinds.lease_name("launch")
    try:
        epoch = kl.acquire(conn, name, "launch-a", 60)
        argvs: list = []
        deps = _launch_deps(argvs, chair_kinds.QueueProof(name, "launch-a", epoch))
        real_run = deps.run

        def run(argv: list) -> tuple[int, str]:
            out = real_run(argv)
            if len(argvs) == 1:  # while the first launch starts, another worker takes the launch kind over
                with conn.transaction():
                    conn.execute("UPDATE leases SET expires_at = '1970-01-01T00:00:00Z' WHERE name = ?", (name,))
                assert kl.acquire(conn, name, "launch-b", 60) == epoch + 1
            return out

        deps = type(deps)(**{**deps.__dict__, "run": run})
        actions = [{"kind": "launch_decompose", "intake_ids": [f"intake/{n}.md"], "epoch": epoch} for n in ("x", "y", "z")]
        results = perform_batched(
            actions, lambda: kl.holds(conn, name, "launch-a", epoch), lambda: epoch,
            lambda batch, current: perform(list(batch), deps, current, False),  # type: ignore[arg-type]
        )
        assert len(argvs) == 1 and "intake/x.md" in argvs[0]
        assert [r["status"] for r in results] == ["done", "fenced", "fenced"]
    finally:
        conn.close()


def test_a_launch_pass_whose_lease_still_holds_performs_every_launch_in_one_perform():
    from agent_tools.chair_queue import perform_batched

    calls: list = []

    def perform_batch(batch, current):
        calls.append([current() for _ in batch])
        return [{"action": a, "status": "done", "reason": ""} for a in batch]

    actions = [{"kind": "launch_epic", "initiative": i, "epoch": 4} for i in ("a", "b")]
    results = perform_batched(actions, lambda: True, lambda: 4, perform_batch)  # type: ignore[arg-type]
    assert calls == [[4, 4]] and [r["status"] for r in results] == ["done", "done"]
    assert perform_batched(actions, lambda: True, lambda: None, lambda b, c: [{"e": c()}]) == [{"e": -2}]  # type: ignore[arg-type,list-item]


def test_a_launch_worker_pass_plans_the_launch_slot_and_hands_it_to_one_batched_perform():
    """`chair_queue.run` for `launch`, one pass, over the tick's own planner: every launch the pass plans goes to
    `perform_batch` in one call, stamped with the worker's epoch; `perform_one` is never used for it."""
    import datetime as dt

    from agent_tools import chair_queue
    from tests.test_chair_plan_kinds import _SCENARIOS

    facts, now = _SCENARIOS["launch fills free lanes"]
    facts = {**facts, "chair_queues": ["launch"]}
    batches: list = []
    singles: list = []

    def perform_batch(batch, current):
        batches.append([(a["kind"], a.get("epoch"), current()) for a in batch])
        return [{"action": a, "status": "done", "reason": ""} for a in batch]

    clock = iter(float(n) for n in range(1000))
    ports = chair_queue.Ports(
        queued=lambda: frozenset({"launch"}), acquire=lambda: 7, renew=lambda e: True, holds=lambda e: e == 7,
        release=lambda e: None, gather=lambda at: facts, perform_one=lambda a, c: singles.append(a) or [],
        wait=lambda s: frozenset(), pause=lambda: None, clock=lambda: next(clock),
        now=lambda: now if isinstance(now, dt.datetime) else dt.datetime.now(dt.UTC), echo=lambda line: None,
        perform_batch=perform_batch,
    )
    assert chair_queue.run("launch", ports, chair_queue.Timing(poll=60, settle=0), once=True, dry_run=False) == 0
    assert singles == [] and len(batches) == 1
    assert batches[0] and any(kind == "launch_epic" for kind, _e, _c in batches[0])
    assert {(e, c) for _k, e, c in batches[0]} == {(7, 7)}
