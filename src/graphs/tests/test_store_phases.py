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
    assert (row["state"], row["outcome"], row["attempts"]) == ("ready", "", 1)


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


@pytest.mark.parametrize("store_url", ["postgres"], indirect=True)
def test_two_workers_claiming_at_once_never_take_the_same_phase(store_url):
    """Two connections, eight phases, eight claims each racing: every phase is taken once (Postgres only, where the
    claims run concurrently; sqlite's in-memory store is one connection)."""
    setup = open_store(store_url, AT)
    store_phases.apply_changes(setup, [_change(f"p{n}", seq=n) for n in range(8)], "chair-loop")
    taken: dict[str, list[str]] = {"w1": [], "w2": []}
    start = threading.Barrier(2)

    def claim_all(name: str) -> None:
        own = open_store(store_url, AT)
        start.wait()
        for _ in range(8):
            found = store_phases.claim_next(own, name, [], 8)
            if found is not None:
                taken[name].append(found.phase)
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
