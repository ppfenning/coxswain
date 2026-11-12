"""The landing record and the land queue in the store."""
import pytest

from harness import landing_store as ls
from harness.store_dialect import connect
from harness.store_migrate import default_modules, migrate

T0 = "2026-11-12T10:00:00Z"
T1 = "2026-11-12T10:01:00Z"
T2 = "2026-11-12T10:05:00Z"
T9 = "2026-11-12T11:00:00Z"


@pytest.fixture
def conn(store_url):
    c = connect(store_url)
    migrate(c, T0, default_modules())
    yield c
    c.close()


def _record(initiative="i1", state="building", updated=T0):
    return {"initiative": initiative, "repo": "~/repos/x", "branch": f"init/{initiative}", "parent": "main",
            "base_sha": "a" * 40, "state": state, "phases": {}, "created_at": T0, "updated_at": updated}


def test_a_record_reads_back_as_written_and_a_second_write_replaces_it(conn):
    assert ls.get_record(conn, "i1") is None
    ls.put_record(conn, _record())
    assert ls.get_record(conn, "i1") == _record()
    changed = {**_record(state="assembled", updated=T1), "phases": {"p1": {"state": "merged", "merge_sha": "b" * 40}}}
    ls.put_record(conn, changed)
    assert ls.get_record(conn, "i1") == changed
    assert conn.query_all("SELECT initiative, state, created_at, updated_at FROM landing_records") == [("i1", "assembled", T0, T1)]


def test_records_list_by_initiative_and_filter_by_state(conn):
    ls.put_record(conn, _record("b", "landed"))
    ls.put_record(conn, _record("a"))
    assert [r["initiative"] for r in ls.list_records(conn)] == ["a", "b"]
    assert [r["initiative"] for r in ls.list_records(conn, states=("landed",))] == ["b"]
    ls.delete_record(conn, "a")
    assert [r["initiative"] for r in ls.list_records(conn)] == ["b"]


def test_item_ids_are_one_per_phase_merge_and_one_per_initiative_land():
    assert ls.item_id(ls.PHASE_MERGE, "i1", "p2") == "phase_merge:i1:p2"
    assert ls.item_id(ls.INITIATIVE_LAND, "i1", "p2") == "initiative_land:i1"
    with pytest.raises(ValueError, match="land queue kind"):
        ls.item_id("phase_pr", "i1")


def test_enqueueing_twice_queues_once(conn):
    assert ls.enqueue(conn, ls.PHASE_MERGE, "i1", phase="p1", repo="r", now=T0, reason="phase built") is True
    assert ls.enqueue(conn, ls.PHASE_MERGE, "i1", phase="p1", repo="r", now=T1, reason="sweep") is False
    [item] = ls.list_items(conn)
    assert (item["id"], item["state"], item["reason"], item["queued_at"], item["attempts"]) == (
        "phase_merge:i1:p1", "queued", "phase built", T0, 0,
    )


def test_only_one_worker_claims_an_item_and_the_other_is_refused(conn):
    ls.enqueue(conn, ls.PHASE_MERGE, "i1", phase="p1", repo="r", now=T0)
    ident = ls.item_id(ls.PHASE_MERGE, "i1", "p1")
    assert ls.claim(conn, ident, "lander-a", now=T1, lease_until=T2) is True
    assert ls.claim(conn, ident, "lander-b", now=T1, lease_until=T2) is False
    item = ls.get_item(conn, ident)
    assert (item["state"], item["claimed_by"], item["attempts"]) == ("claimed", "lander-a", 1)


def test_an_item_whose_worker_died_is_reclaimed_once_its_lease_lapses(conn):
    ls.enqueue(conn, ls.PHASE_MERGE, "i1", phase="p1", repo="r", now=T0)
    ident = ls.item_id(ls.PHASE_MERGE, "i1", "p1")
    assert ls.claim(conn, ident, "lander-a", now=T1, lease_until=T2)
    assert ls.claim(conn, ident, "lander-b", now=T1, lease_until=T9) is False, "the lease still stands"
    assert ls.claim(conn, ident, "lander-b", now=T9, lease_until="2026-11-12T11:05:00Z") is True
    assert ls.claim(conn, ident, "lander-c", now=T9, lease_until="2026-11-12T11:05:00Z") is False, "reclaimed once"
    assert ls.finish(conn, ident, "lander-a", now=T9) is False, "the dead worker's late answer is ignored"
    assert ls.finish(conn, ident, "lander-b", now=T9) is True
    assert ls.get_item(conn, ident)["state"] == "done"


def test_a_renewed_claim_is_not_reclaimed(conn):
    ls.enqueue(conn, ls.INITIATIVE_LAND, "i1", repo="r", parent="main", now=T0)
    ident = ls.item_id(ls.INITIATIVE_LAND, "i1")
    assert ls.claim(conn, ident, "lander-a", now=T1, lease_until=T2)
    assert ls.renew(conn, ident, "lander-a", now=T2, lease_until="2026-11-12T12:00:00Z") is True
    assert ls.renew(conn, ident, "lander-b", now=T2, lease_until="2026-11-12T12:00:00Z") is False
    assert ls.claim(conn, ident, "lander-b", now=T9, lease_until="2026-11-12T12:00:00Z") is False


def test_a_failed_item_keeps_its_error_and_history_when_it_is_queued_again(conn):
    ls.enqueue(conn, ls.PHASE_MERGE, "i1", phase="p1", repo="r", now=T0)
    ident = ls.item_id(ls.PHASE_MERGE, "i1", "p1")
    ls.claim(conn, ident, "lander-a", now=T1, lease_until=T2)
    assert ls.fail(conn, ident, "lander-a", now=T1, error="merge conflict in src/a.py") is True
    failed = ls.get_item(conn, ident)
    assert (failed["state"], failed["last_error"], failed["lease_until"]) == ("failed", "merge conflict in src/a.py", "")
    assert ls.claim(conn, ident, "lander-b", now=T9, lease_until=T9) is False, "failed stays failed until queued again"
    assert ls.enqueue(conn, ls.PHASE_MERGE, "i1", phase="p1", repo="r", now=T2, reason="rectify task approved") is True
    again = ls.get_item(conn, ident)
    assert (again["state"], again["queued_at"], again["attempts"], again["last_error"]) == (
        "queued", T2, 1, "merge conflict in src/a.py",
    )


def test_a_released_item_goes_back_to_queued_without_counting_an_attempt(conn):
    ls.enqueue(conn, ls.INITIATIVE_LAND, "i1", repo="r", parent="main", now=T0)
    ident = ls.item_id(ls.INITIATIVE_LAND, "i1")
    ls.claim(conn, ident, "lander-a", now=T1, lease_until=T2)
    assert ls.release(conn, ident, "lander-a", now=T1, error="main is landing another initiative") is True
    item = ls.get_item(conn, ident)
    assert (item["state"], item["attempts"], item["claimed_by"]) == ("queued", 0, "")


def test_claimable_is_oldest_first_and_includes_lapsed_claims(conn):
    ls.enqueue(conn, ls.PHASE_MERGE, "i2", phase="p1", repo="r", now=T1)
    ls.enqueue(conn, ls.PHASE_MERGE, "i1", phase="p1", repo="r", now=T0)
    ls.enqueue(conn, ls.PHASE_MERGE, "i3", phase="p1", repo="r", now=T0)
    ls.claim(conn, "phase_merge:i3:p1", "lander-a", now=T1, lease_until=T2)
    items = ls.list_items(conn)
    assert [i["id"] for i in ls.claimable(items, T1)] == ["phase_merge:i1:p1", "phase_merge:i2:p1"]
    assert [i["id"] for i in ls.claimable(items, T9)] == ["phase_merge:i1:p1", "phase_merge:i3:p1", "phase_merge:i2:p1"]
    assert ls.oldest_waiting(items)["id"] == "phase_merge:i1:p1"
    assert ls.oldest_waiting([]) is None
