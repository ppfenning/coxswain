from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agent_tools import chair, chair_report, store_cli
from agent_tools.chair_run import RunDeps, run

TTL = 0.15
INTERVAL = 0.02
HOST = "box"


class ManualClock:
    """A clock only the test moves, so a lease's expiry never depends on how long the runner took to schedule a thread."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class FakeLeaseStore:
    """A one-row lease store with the real fence: renew needs the held holder and epoch, and a steal takes a higher epoch.

    Expiry is read off `now`, real time unless a test swaps in a `ManualClock`. `changed` is notified on every renew and
    refusal, so a test waits for a beat with `wait_for` instead of sleeping and hoping one landed."""

    def __init__(self) -> None:
        self.guard = threading.Lock()
        self.changed = threading.Condition(self.guard)
        self.now: Callable[[], float] = time.monotonic
        self.holder: str | None = None
        self.epoch = 0
        self.expires = 0.0
        self.renewals: dict[str, int] = {}
        self.refusals: dict[str, int] = {}

    def live(self) -> bool:
        with self.guard:
            return self.now() < self.expires

    def wait_for(self, predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
        """True once `predicate` holds under the store's lock; False after `timeout`, a bound on a hang, not a schedule."""
        with self.changed:
            return self.changed.wait_for(predicate, timeout)

    def acquire(self, _runs_dir, _name, holder, _ttl, steal=False):
        with self.guard:
            now = self.now()
            if self.holder not in (None, holder) and now < self.expires and not steal:
                return store_cli.LeaseRefused(self.epoch, self.holder)
            if self.holder != holder:
                self.epoch += 1
            self.holder, self.expires = holder, now + TTL
            return store_cli.LeaseGranted(self.epoch, holder)

    def renew(self, _runs_dir, _name, holder, epoch, _ttl):
        with self.guard:
            if holder != self.holder or epoch != self.epoch:
                self.refusals[holder] = self.refusals.get(holder, 0) + 1
                self.changed.notify_all()
                return store_cli.LeaseRefused(self.epoch, self.holder)
            self.renewals[holder] = self.renewals.get(holder, 0) + 1
            self.expires = self.now() + TTL
            self.changed.notify_all()
            return store_cli.LeaseGranted(self.epoch, holder)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeLeaseStore:
    fake = FakeLeaseStore()
    monkeypatch.setattr(store_cli, "lease_acquire", fake.acquire)
    monkeypatch.setattr(store_cli, "lease_renew", fake.renew)
    return fake


def _loop_deps(runs_dir, session: str, pid: int, during_tick) -> RunDeps:
    """A loop whose one tick runs `during_tick` and then fails, so only the beat thread can keep the lease alive."""

    def gather(_facts_deps, _now):
        during_tick()
        raise RuntimeError("tick over")

    return RunDeps(
        facts_deps=object(),
        exec_deps=SimpleNamespace(record=lambda _action: None),
        report_deps=chair_report.Deps(echo=lambda _line: None, notify=None),
        beat=lambda: None,
        current_epoch=lambda: 1,
        holds=lambda: False,
        release=lambda: None,
        sleep=lambda _seconds: None,
        now=lambda: datetime(2026, 10, 6, tzinfo=UTC),
        gather=gather,
        meter_doc=lambda: None,
        install_sigterm=lambda: (lambda: None),
        lease_beat=lambda: chair.renew_lease(runs_dir, session, pid, HOST),
        lease_beat_interval=INTERVAL,
    )


def test_a_tick_longer_than_the_lease_ttl_leaves_the_lease_live(tmp_path, store):
    clock = ManualClock()
    store.now = clock
    assert chair.acquire_lease(tmp_path, "a", 1, HOST) == ""
    holder = chair.lease_holder("a", 1, HOST)
    at_end: list[bool] = []

    def long_tick() -> None:
        # Four TTLs of lease time pass in half-TTL steps, each only after one more beat landed; without the beat the
        # lease would lapse on the third step.
        for _ in range(8):
            seen = store.renewals.get(holder, 0)
            assert store.wait_for(lambda seen=seen: store.renewals.get(holder, 0) > seen)
            clock.t += TTL / 2
        at_end.append(store.live())

    run(True, 60, False, _loop_deps(tmp_path, "a", 1, long_tick))
    assert at_end == [True]
    assert store.renewals[holder] >= 8


def test_after_the_loop_exits_the_beat_stops_and_the_lease_goes_stale(tmp_path, store):
    assert chair.acquire_lease(tmp_path, "a", 1, HOST) == ""
    holder = chair.lease_holder("a", 1, HOST)
    run(True, 60, False, _loop_deps(tmp_path, "a", 1, lambda: time.sleep(INTERVAL * 3)))
    time.sleep(INTERVAL * 3)  # a beat already past its wait may still land
    # On a loaded runner no beat may land before the loop exits; the property is that beats stop, not that one landed.
    settled = store.renewals.get(holder, 0)
    time.sleep(TTL * 2)
    assert store.renewals.get(holder, 0) == settled
    assert not store.live()


def test_the_older_epoch_is_rejected_once_a_newer_loop_holds_the_lease(tmp_path, store):
    store.now = ManualClock()  # no lease time passes: a slow runner cannot expire the newer loop's lease under the test
    older, newer = tmp_path / "older", tmp_path / "newer"
    older.mkdir()
    newer.mkdir()
    older_holder, newer_holder = chair.lease_holder("a", 1, HOST), chair.lease_holder("b", 2, HOST)
    assert chair.acquire_lease(older, "a", 1, HOST) == ""
    assert chair.acquire_lease(newer, "b", 2, HOST, steal=True) == ""
    assert store.holder == newer_holder and store.epoch == 2

    def newer_beats() -> None:
        assert chair.renew_lease(newer, "b", 2, HOST) == ""
        # The older loop's beat has been refused by the time the tick ends, however late its thread was scheduled.
        assert store.wait_for(lambda: store.refusals.get(older_holder, 0) >= 1)
        for _ in range(9):
            assert chair.renew_lease(newer, "b", 2, HOST) == ""

    run(True, 60, False, _loop_deps(older, "a", 1, newer_beats))
    assert store.refusals[older_holder] == 1  # one rejected beat, then the thread stopped
    assert store.renewals.get(older_holder, 0) == 0
    assert store.renewals[newer_holder] == 10
    assert store.holder == newer_holder and store.live()


def test_a_dry_run_starts_no_beat(tmp_path, store):
    calls: list[int] = []
    deps = _loop_deps(tmp_path, "a", 1, lambda: time.sleep(INTERVAL * 4))
    deps = RunDeps(**{**deps.__dict__, "lease_beat": lambda: calls.append(1)})
    run(True, 60, True, deps)
    assert calls == []
