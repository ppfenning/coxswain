"""The lease keeper: the pure rules, and the keeper over a real store."""
import pytest

from harness import lease_keeper, store_lease
from harness.store_dialect import connect
from harness.store_migrate import default_modules, migrate

T0 = "2026-11-21T10:00:00Z"
T1 = "2026-11-21T10:05:00Z"
LATE = "2026-11-21T11:00:00Z"


@pytest.fixture
def conn(store_url):
    c = connect(store_url)
    migrate(c, T0, default_modules())
    yield c
    c.close()


@pytest.mark.parametrize(("outcome", "step"), [(True, "held"), (False, "lost"), (None, "retry")])
def test_a_refused_renew_loses_the_lease_and_one_that_raised_says_nothing(outcome, step):
    assert lease_keeper.renew_step(outcome) == step


def test_a_lease_is_renewed_three_times_in_each_of_its_lifetimes():
    assert lease_keeper.beat_s(900) == 300
    assert lease_keeper.beat_s(0) > 0, "never a busy loop"


def test_confirm_extends_a_held_lease_and_never_one_another_holder_took(conn):
    held = store_lease.acquire(conn, "b", "me", T0, 900)
    clock = [T1]
    keeper = lease_keeper.Keeper(conn, "b", "me", int(held.epoch or 0), ttl=900, clock=lambda: clock[0], interval=3600)
    assert keeper.confirm() is True
    assert not store_lease.acquire(conn, "b", "other", "2026-11-21T10:16:00Z", 900).ok, "renewed past its first 900 s"
    assert store_lease.acquire(conn, "b", "other", T1, 900, steal=True).ok
    assert keeper.confirm() is False and keeper.lost.is_set() and "no longer me's" in keeper.why
    clock[0] = T0
    assert keeper.confirm() is False, "a lost lease stays lost"


def test_a_lapsed_lease_is_not_revived(conn):
    held = store_lease.acquire(conn, "b", "me", T0, 900)
    keeper = lease_keeper.Keeper(conn, "b", "me", int(held.epoch or 0), ttl=900, clock=lambda: LATE, interval=3600)
    assert keeper.confirm() is False and keeper.lost.is_set()


def test_the_thread_keeps_the_lease_until_stopped(conn):
    import time
    from datetime import UTC, datetime

    def wall() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    held = store_lease.acquire(conn, "b", "me", wall(), 2)
    keeper = lease_keeper.Keeper(conn, "b", "me", int(held.epoch or 0), ttl=2, clock=wall, interval=0.2).start()
    try:
        time.sleep(3)
        assert not store_lease.acquire(conn, "b", "other", wall(), 2).ok
        assert not keeper.lost.is_set()
    finally:
        keeper.stop()
    time.sleep(2.5)
    assert store_lease.acquire(conn, "b", "other", wall(), 2).ok, "once stopped, the lease lapses on its own"
