from agent_tools import land_lease
from harness import store_lease
from harness.land_claims import claimed_tasks, read_claimed_tasks

NOW = "2026-11-06T12:00:00Z"
LIVE = "2026-11-06T13:00:00Z"
GONE = "2026-11-06T11:00:00Z"


def row(name, expires_at):
    return {"name": name, "holder": "h", "epoch": 1, "heartbeat_at": GONE, "expires_at": expires_at}


def test_a_live_lease_for_the_initiative_claims_its_task():
    rows = [row(land_lease.task_lease_name("i1", "t1"), LIVE)]
    assert claimed_tasks(rows, "i1", NOW) == {"t1"}


def test_an_expired_lease_claims_nothing():
    rows = [row(land_lease.task_lease_name("i1", "t1"), GONE), row(land_lease.task_lease_name("i1", "t2"), NOW)]
    assert claimed_tasks(rows, "i1", NOW) == frozenset()


def test_a_lease_for_another_initiative_claims_nothing():
    rows = [row(land_lease.task_lease_name("i2", "t1"), LIVE)]
    assert claimed_tasks(rows, "i1", NOW) == frozenset()


def test_repo_phase_and_bare_task_leases_claim_nothing():
    rows = [row("land:repo:/work/repo", LIVE), row("land:phase:i1/p1", LIVE), row("land:t1", LIVE), row("epic", LIVE)]
    assert claimed_tasks(rows, "i1", NOW) == frozenset()


def test_the_name_round_trips_through_build_and_parse():
    assert land_lease.parse_task_lease_name(land_lease.task_lease_name("i-1", "t_1")) == ("i-1", "t_1")
    assert land_lease.parse_task_lease_name("land:t1") is None
    assert land_lease.parse_task_lease_name("land:task:i1/") is None


def test_the_reader_returns_only_the_live_claim_of_the_initiative(store_conn):
    store_lease.acquire(store_conn, land_lease.task_lease_name("i1", "live"), "h", NOW, 3600)
    store_lease.acquire(store_conn, land_lease.task_lease_name("i1", "old"), "h", "2026-11-06T09:00:00Z", 60)
    store_lease.acquire(store_conn, land_lease.task_lease_name("i2", "other"), "h", NOW, 3600)
    store_lease.acquire(store_conn, "land:bare", "h", NOW, 3600)
    assert read_claimed_tasks(store_conn, "i1", NOW) == {"live"}


def test_run_under_land_lease_names_the_lease_by_initiative_when_given_one():
    from pathlib import Path

    from agent_tools import store_cli

    names = []

    def acquire(runs_dir, name, holder, ttl):
        names.append(name)
        return store_cli.LeaseGranted(1, holder)

    def release(runs_dir, name, holder, epoch):
        return store_cli.LeaseReleased()

    land_lease.run_under_land_lease(Path("/r"), "t1", "h", 60, lambda epoch: None, acquire=acquire, release=release, initiative="i1")
    land_lease.run_under_land_lease(Path("/r"), "t1", "h", 60, lambda epoch: None, acquire=acquire, release=release)
    assert names == ["land:task:i1/t1", "land:t1"]
