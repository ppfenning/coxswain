import threading
import time

import pytest

from agent_tools import land_merge_lock, land_repo_lease, store_cli


class FakeStore:
    """In-memory lease store; one lock makes acquire and release atomic across threads."""

    def __init__(self):
        self.leases = {}
        self.epoch = 0
        self.released = []
        self.lock = threading.Lock()

    def acquire(self, runs_dir, name, holder, ttl):
        with self.lock:
            current = self.leases.get(name)
            if current is not None:
                return store_cli.LeaseRefused(current[1], current[0])
            self.epoch += 1
            self.leases[name] = (holder, self.epoch)
            return store_cli.LeaseGranted(self.epoch, holder)

    def release(self, runs_dir, name, holder, epoch):
        with self.lock:
            self.released.append(name)
            self.leases.pop(name, None)
            return store_cli.LeaseReleased()


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def hold(store, holder="a", timeout=5, clock=None):
    clock = clock or FakeClock()
    return land_merge_lock.hold("runs", "/repo", holder, 30, timeout, sleep=clock.sleep, clock=clock.clock,
                                acquire=store.acquire, release=store.release)


def test_grant_then_release_on_exit():
    store = FakeStore()
    with hold(store) as got:
        assert got == land_merge_lock.Held(1, "a")
        assert "merge:/repo" in store.leases
    assert store.leases == {}
    assert store.released == ["merge:/repo"]


def test_release_on_exception():
    store = FakeStore()
    with pytest.raises(RuntimeError), hold(store):
        raise RuntimeError("boom")
    assert store.leases == {}


def test_two_threads_are_never_inside_together():
    store = FakeStore()
    inside, peak, results = [0], [0], []
    guard = threading.Lock()

    def worker(name):
        with land_merge_lock.hold("runs", "/repo", name, 30, 10, poll=0.001,
                                  acquire=store.acquire, release=store.release) as got:
            results.append(got)
            with guard:
                inside[0] += 1
                peak[0] = max(peak[0], inside[0])
            time.sleep(0.02)
            with guard:
                inside[0] -= 1

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1
    assert len(results) == 2
    assert all(isinstance(r, land_merge_lock.Held) for r in results)


def test_timeout_names_the_holder_and_releases_nothing():
    store = FakeStore()
    store.acquire("runs", "merge:/repo", "other", 30)
    clock = FakeClock()
    with hold(store, holder="me", timeout=2, clock=clock) as got:
        assert got == land_merge_lock.NotHeld("timeout", "other")
    assert store.released == []
    assert store.leases == {"merge:/repo": ("other", 1)}
    assert clock.now >= 2


def test_outage_is_not_held_and_does_not_raise():
    store = FakeStore()

    def down(*args):
        return store_cli.LeaseError("down")

    def unavailable(*args):
        return store_cli.NotAvailable("no store")

    with land_merge_lock.hold("runs", "/repo", "a", 30, 5, acquire=down, release=store.release) as got:
        assert got == land_merge_lock.NotHeld("store outage: down")
    with land_merge_lock.hold("runs", "/repo", "a", 30, 5, acquire=unavailable, release=store.release) as got:
        assert got == land_merge_lock.NotHeld("store outage: no store")
    assert store.released == []


def test_lock_name_differs_from_every_slot_name():
    assert land_merge_lock.merge_lock_name("/repo") == "merge:/repo"
    slots = {land_repo_lease.lease_name("/repo")} | {land_repo_lease.slot_lease_name("/repo", k) for k in range(4)}
    assert land_merge_lock.merge_lock_name("/repo") not in slots
