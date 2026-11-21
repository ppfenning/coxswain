"""`store_phases` against a real store (sqlite, and Postgres when COXSWAIN_TEST_PG_URL names one): the chair's writes,
the claim, the heartbeat, the fenced finish and release, and the registry. Times are the store's own, so a lapsed lease
is made by writing a past `lease_until` into the row, as the store's clock moving past it would leave it."""

import threading

import pytest

from harness import store_phases
from harness.store_migrate import open_store
from harness.store_phases import Claim

AT = "2026-11-20T00:00:00Z"
PAST = "2000-01-01T00:00:00Z"


def _change(phase="p1", *, op="insert", requires=(), seq=0, **extra):
    return {"op": op, "initiative": "ship", "phase": phase, "kind": "build", "repo": "~/src/app",
            "requires": list(requires), "band": 1, "seq": seq, **extra}


@pytest.fixture
def conn(store_url):
    c = open_store(store_url, AT)
    yield c
    c.close()


def _set(conn, phase, **columns):
    p = conn.dialect.placeholder
    sets = ", ".join(f"{k} = {p}" for k in columns)
    conn.execute(f"UPDATE phase_queue SET {sets} WHERE initiative = {p} AND phase = {p}", (*columns.values(), "ship", phase))


def test_the_chair_insert_is_a_ready_row_and_a_repeat_writes_nothing(conn):
    assert store_phases.apply_changes(conn, [_change(requires=["docker", "repo:app"])], "chair-loop") == 1
    assert store_phases.apply_changes(conn, [_change(requires=["docker", "repo:app"])], "chair-loop") == 0
    row = store_phases.get_row(conn, "ship", "p1")
    assert row is not None
    assert (row["state"], row["requires_json"], row["claim_epoch"], row["attempts"]) == ("ready", ["docker", "repo:app"], 0, 0)
    assert row["queued_at"] == row["ready_at"] and row["queued_at"].endswith("Z")


def test_an_update_never_touches_a_claimed_row(conn):
    store_phases.apply_changes(conn, [_change()], "chair-loop")
    assert store_phases.apply_changes(conn, [_change(op="update", seq=7)], "chair-loop") == 1
    assert store_phases.get_row(conn, "ship", "p1")["seq"] == 7
    assert store_phases.claim_next(conn, "w1", [], 1) is not None
    assert store_phases.apply_changes(conn, [_change(op="update", seq=9, requires=["os:macos"])], "chair-loop") == 0
    assert store_phases.get_row(conn, "ship", "p1")["seq"] == 7


def test_a_claim_takes_the_first_row_the_worker_meets_and_counts_the_attempt(conn):
    store_phases.apply_changes(conn, [_change("mac", requires=["os:macos"]), _change("any", seq=1)], "chair-loop")
    claim = store_phases.claim_next(conn, "w1", ["os:linux"], 1)
    assert claim is not None and (claim.phase, claim.epoch) == ("any", 1)
    row = store_phases.get_row(conn, "ship", "any")
    assert (row["state"], row["claimed_by"], row["attempts"], row["lease_until"]) == ("claimed", "w1", 1, claim.lease_until)
    assert store_phases.claim_next(conn, "w1", ["os:linux"], 1) is None  # the macOS phase stays ready
    assert store_phases.get_row(conn, "ship", "mac")["state"] == "ready"
    assert store_phases.claim_next(conn, "w2", ["os:macos"], 1).phase == "mac"


def test_no_free_lane_claims_nothing(conn):
    store_phases.apply_changes(conn, [_change()], "chair-loop")
    assert store_phases.claim_next(conn, "w1", [], 0) is None
    assert store_phases.get_row(conn, "ship", "p1")["state"] == "ready"


def test_a_heartbeat_renews_only_the_claim_it_holds(conn):
    store_phases.apply_changes(conn, [_change()], "chair-loop")
    claim = store_phases.claim_next(conn, "w1", [], 1)
    assert store_phases.heartbeat(conn, claim, "w1") is not None
    assert store_phases.heartbeat(conn, claim, "w2") is None
    stale = Claim(claim.initiative, claim.phase, claim.epoch - 1, claim.lease_until, claim.row)
    assert store_phases.heartbeat(conn, stale, "w1") is None


def test_a_lapsed_claim_is_claimed_again_and_the_stale_worker_writes_nothing(conn):
    store_phases.apply_changes(conn, [_change()], "chair-loop")
    first = store_phases.claim_next(conn, "w1", [], 1)
    _set(conn, "p1", lease_until=PAST)
    assert store_phases.heartbeat(conn, first, "w1") is None  # a lapsed lease is never revived
    second = store_phases.claim_next(conn, "w2", [], 1)
    assert second is not None and second.epoch == first.epoch + 1
    assert not store_phases.finish(conn, first, "w1", "approved", "late")
    assert not store_phases.release(conn, first, "w1", "late", 60)
    assert not store_phases.set_run(conn, first, "w1", "ship.p1-9")
    row = store_phases.get_row(conn, "ship", "p1")
    assert (row["state"], row["claimed_by"], row["claim_epoch"], row["attempts"], row["run_id"]) == ("claimed", "w2", 2, 2, "")


def test_finish_closes_the_claim_once(conn):
    store_phases.apply_changes(conn, [_change()], "chair-loop")
    claim = store_phases.claim_next(conn, "w1", [], 1)
    assert store_phases.set_run(conn, claim, "w1", "ship.p1-1")
    assert store_phases.finish(conn, claim, "w1", "approved", "every task of the phase is approved")
    assert not store_phases.finish(conn, claim, "w1", "approved", "again")
    row = store_phases.get_row(conn, "ship", "p1")
    assert (row["state"], row["outcome"], row["lease_until"], row["run_id"]) == ("finished", "approved", "", "ship.p1-1")
    assert store_phases.claim_next(conn, "w1", [], 1) is None


def test_a_reopen_needs_the_finish_the_chair_saw(conn):
    store_phases.apply_changes(conn, [_change()], "chair-loop")
    claim = store_phases.claim_next(conn, "w1", [], 1)
    store_phases.finish(conn, claim, "w1", "quarantined", "t1 quarantined")
    assert store_phases.apply_changes(conn, [_change(op="reopen", epoch=0)], "chair-loop") == 0
    assert store_phases.apply_changes(conn, [_change(op="reopen", epoch=1)], "chair-loop") == 1
    assert store_phases.apply_changes(conn, [_change(op="reopen", epoch=1)], "chair-loop") == 0
    row = store_phases.get_row(conn, "ship", "p1")
    assert (row["state"], row["outcome"], row["attempts"]) == ("ready", "", 0)


def test_a_reopened_phase_starts_a_new_episode_even_after_three_claims(conn):
    """An approved phase that took three claims, then got new work: the reopen is claimable at once, not exhausted."""
    store_phases.apply_changes(conn, [_change()], "chair-loop")
    for epoch in (1, 2, 3):
        claim = store_phases.claim_next(conn, "w1", [], 1)
        if epoch < 3:
            store_phases.release(conn, claim, "w1", "tasks left ready", 0)
    store_phases.finish(conn, claim, "w1", "approved", "every task of the phase is approved")
    _set(conn, "p1", ready_at="2999-01-01T00:00:00Z")  # a backoff left behind is cleared too
    assert store_phases.apply_changes(conn, [_change(op="reopen", epoch=3)], "chair-loop") == 1
    assert store_phases.get_row(conn, "ship", "p1")["attempts"] == 0
    again = store_phases.claim_next(conn, "w2", [], 1)
    assert again is not None and (again.epoch, again.row["attempts"]) == (4, 1)


def test_a_release_waits_out_its_backoff_before_another_claim(conn):
    store_phases.apply_changes(conn, [_change()], "chair-loop")
    claim = store_phases.claim_next(conn, "w1", [], 1)
    assert store_phases.release(conn, claim, "w1", "tasks left ready", 60)
    row = store_phases.get_row(conn, "ship", "p1")
    assert (row["state"], row["claimed_by"], row["attempts"], row["reason"]) == ("ready", "", 1, "tasks left ready")
    assert row["ready_at"] > row["updated_at"]
    assert store_phases.claim_next(conn, "w2", [], 1) is None
    _set(conn, "p1", ready_at=PAST)
    assert store_phases.claim_next(conn, "w2", [], 1).epoch == 2


def test_live_claims_are_the_unlapsed_ones(conn):
    store_phases.apply_changes(conn, [_change("a"), _change("b", seq=1)], "chair-loop")
    store_phases.claim_next(conn, "w1", [], 1)
    store_phases.claim_next(conn, "w2", [], 1)
    _set(conn, "b", lease_until=PAST)
    assert [r["phase"] for r in store_phases.live_claims(conn)] == ["a"]


def test_the_registry_row_is_the_worker_s_own_and_a_repeat_keeps_its_start(conn):
    first = store_phases.register(conn, "w1", ["os:linux", "docker", "docker"], 2, "up", "1.0", {"lane": []})
    again = store_phases.register(conn, "w1", ["os:linux"], 2, "up", "1.0", {})
    assert again["started_at"] == first["started_at"]
    store_phases.register(conn, "w1", ["os:linux"], 0, "stopping", "1.0", {})
    (row,) = store_phases.list_workers(conn)
    assert (row["name"], row["capabilities_json"], row["lanes"], row["state"]) == ("w1", ["os:linux"], 0, "stopping")
    assert first["capabilities_json"] == ["docker", "os:linux"]


def test_a_refused_launch_s_release_gives_the_attempt_back(conn):
    store_phases.apply_changes(conn, [_change()], "chair-loop")
    claim = store_phases.claim_next(conn, "w1", [], 1)
    assert store_phases.release(conn, claim, "w1", "the launch was refused", 0, refund=True)
    assert store_phases.get_row(conn, "ship", "p1")["attempts"] == 0
    assert store_phases.claim_next(conn, "w1", [], 1).epoch == 2


def test_a_phase_out_of_attempts_is_never_claimed_and_the_chair_finishes_it_failed(conn):
    store_phases.apply_changes(conn, [_change()], "chair-loop")
    for epoch in (1, 2, 3):
        claim = store_phases.claim_next(conn, f"w{epoch}", [], 1)
        assert claim is not None and claim.epoch == epoch
        _set(conn, "p1", lease_until=PAST)  # each holder lost its lease without a verdict
    assert store_phases.claim_next(conn, "w4", [], 1) is None
    assert store_phases.apply_changes(conn, [_change(op="exhaust", reason="3 claims")], "chair-loop") == 1
    assert store_phases.apply_changes(conn, [_change(op="exhaust", reason="3 claims")], "chair-loop") == 0
    row = store_phases.get_row(conn, "ship", "p1")
    assert (row["state"], row["outcome"], row["attempts"]) == ("finished", "failed", 3)


def test_an_exhaust_never_takes_a_claim_its_holder_still_has(conn):
    store_phases.apply_changes(conn, [_change()], "chair-loop")
    store_phases.claim_next(conn, "w1", [], 1)
    _set(conn, "p1", attempts=3)
    assert store_phases.apply_changes(conn, [_change(op="exhaust", reason="x")], "chair-loop") == 0
    assert store_phases.get_row(conn, "ship", "p1")["state"] == "claimed"


def test_a_withdrawn_phase_is_not_claimed_and_comes_back_on_reopen(conn):
    store_phases.apply_changes(conn, [_change()], "chair-loop")
    assert store_phases.apply_changes(conn, [_change(op="withdraw", reason="its initiative is held")], "chair-loop") == 1
    assert store_phases.get_row(conn, "ship", "p1")["state"] == "cancelled"
    assert store_phases.claim_next(conn, "w1", [], 1) is None
    assert store_phases.apply_changes(conn, [_change(op="reopen", epoch=0)], "chair-loop") == 1
    assert store_phases.claim_next(conn, "w1", [], 1) is not None
    assert store_phases.apply_changes(conn, [_change(op="withdraw", reason="held")], "chair-loop") == 0  # claimed now


def test_phases_no_one_here_can_meet_never_stand_in_front_of_one_it_can(conn):
    """Forty macOS phases ahead of one any worker can run: a Linux worker pages past them to it."""
    blocked = [_change(f"mac{n:02d}", seq=n, requires=["os:macos"]) for n in range(40)]
    store_phases.apply_changes(conn, [*blocked, _change("any", seq=99)], "chair-loop")
    claim = store_phases.claim_next(conn, "w1", ["os:linux"], 1)
    assert claim is not None and claim.phase == "any"


@pytest.mark.parametrize("store_url", ["postgres"], indirect=True)
def test_a_claimer_skips_a_row_another_holds_locked_instead_of_waiting(store_url):
    """One transaction holds the first phase locked; a claim taken meanwhile gets the second, at once."""
    setup = open_store(store_url, AT)
    store_phases.apply_changes(setup, [_change("p0", seq=0), _change("p1", seq=1)], "chair-loop")
    holder = open_store(store_url, AT)
    other = open_store(store_url, AT)
    try:
        with holder.transaction():
            holder.query_one("SELECT phase FROM phase_queue WHERE phase = 'p0' FOR UPDATE")
            claim = store_phases.claim_next(other, "w2", [], 1)
            assert claim is not None and claim.phase == "p1"
    finally:
        holder.close()
        other.close()
        setup.close()


@pytest.mark.parametrize("store_url", ["postgres"], indirect=True)
def test_two_workers_claiming_at_once_never_take_the_same_phase(store_url):
    """Two connections, eight phases, each claiming until no ready row is left: every phase is taken exactly once
    (Postgres only, where the claims run concurrently; sqlite's in-memory store is one connection). A claim that finds
    nothing while rows are left (the other holds the last one locked) just tries again, as a worker's poll does."""
    setup = open_store(store_url, AT)
    store_phases.apply_changes(setup, [_change(f"p{n}", seq=n) for n in range(8)], "chair-loop")
    taken: dict[str, list[str]] = {"w1": [], "w2": []}
    start = threading.Barrier(2)

    def claim_all(name: str) -> None:
        own = open_store(store_url, AT)
        start.wait()
        for _ in range(200):
            found = store_phases.claim_next(own, name, [], 8)
            if found is not None:
                taken[name].append(found.phase)
            elif not [r for r in store_phases.list_rows(own) if r["state"] == "ready"]:
                break
        own.close()

    threads = [threading.Thread(target=claim_all, args=(name,)) for name in taken]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    every = taken["w1"] + taken["w2"]
    assert sorted(every) == [f"p{n}" for n in range(8)]
    assert {r["claim_epoch"] for r in store_phases.list_rows(setup)} == {1}
    setup.close()


def _step(task, kind, sha="", run="ship.p1-1", epoch=1):
    from harness.store_steps import step_key, step_row

    return step_row(run_id=run, step=step_key("task", task, kind, 0), initiative="ship", phase="p1", task_id=task,
                    kind=kind, round_=0, ok=True, verdict="", result={}, finished_at="", started_at="",
                    subject_sha=sha, worker="w1", claim_epoch=epoch)


def test_a_lane_writes_its_step_rows_only_while_its_worker_holds_the_claim(conn):
    from harness.store_steps import steps_for

    store_phases.apply_changes(conn, [_change()], "chair-loop")
    claim = store_phases.claim_next(conn, "w1", [], 1)
    assert store_phases.held(conn, "ship", "p1", "w1", claim.epoch)
    assert store_phases.put_steps_fenced(conn, [_step("t1", "rollup", "a1")], "w1", claim.epoch)
    assert store_phases.put_steps_fenced(conn, [_step("t1", "rollup", "a1")], "w1", claim.epoch)  # a repeat: no change
    (row,) = steps_for(conn, "ship.p1-1")
    assert row["finished_at"].endswith("Z") and row["started_at"] == row["finished_at"] and row["subject_sha"] == "a1"
    assert not store_phases.put_steps_fenced(conn, [_step("t2", "rollup")], "w2", claim.epoch)  # not its claim
    assert not store_phases.put_steps_fenced(conn, [_step("t2", "rollup")], "w1", claim.epoch + 1)
    _set(conn, "p1", lease_until=PAST)  # the store's clock moved past the lease
    assert not store_phases.held(conn, "ship", "p1", "w1", claim.epoch)
    assert not store_phases.put_steps_fenced(conn, [_step("t2", "rollup")], "w1", claim.epoch)
    assert [r["step"] for r in steps_for(conn, "ship.p1-1")] == ["task/t1/rollup"]


def test_a_lane_stops_holding_its_claim_once_the_lease_is_inside_the_margin(conn):
    store_phases.apply_changes(conn, [_change()], "chair-loop")
    claim = store_phases.claim_next(conn, "w1", [], 1)
    assert store_phases.held(conn, "ship", "p1", "w1", claim.epoch, margin_s=15)
    assert not store_phases.held(conn, "ship", "p1", "w1", claim.epoch, margin_s=3600)


def test_the_rollups_and_the_last_review_round_of_a_phase_are_read_back(conn):
    store_phases.apply_changes(conn, [_change()], "chair-loop")
    claim = store_phases.claim_next(conn, "w1", [], 1)
    store_phases.put_steps_fenced(conn, [_step("t1", "rollup", "a1"), _step("t1", "checks")], "w1", claim.epoch)
    rows = store_phases.rollup_rows(conn, ["ship"])
    assert [(r["phase"], r["task_id"], r["subject_sha"]) for r in rows] == [("p1", "t1", "a1")]
    assert store_phases.rollup_rows(conn, ["other"]) == [] and store_phases.rollup_rows(conn, []) == []
    assert store_phases.last_round(conn, "ship", "p1") is None
    p = conn.dialect.placeholder
    for number, verdict in ((1, "revise"), (2, "approve")):
        conn.execute(
            f"INSERT INTO review_rounds VALUES ({', '.join([p] * 13)})",
            ("ship", "p1", number, "h", verdict, "", "{}", "", "r", "w1", 1, 0.0, AT),
        )
    assert store_phases.last_round(conn, "ship", "p1") == (2, "approve")


def test_a_machine_with_no_thread_budget_reads_zero(conn):
    assert store_phases.host_threads(conn, "nobody") == 0
