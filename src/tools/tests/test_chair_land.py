import threading
import time
from pathlib import Path

from agent_tools import chair_land, land_repo_lease, store_cli
from agent_tools.ci_gate import CiGate

PAUSED = CiGate(True, "forge incident")


class FakeTime:
    """The clock moves only when the worker waits; a wait blocks until the test grants a tick."""

    def __init__(self) -> None:
        self.now = 0.0
        self.ticks = threading.Semaphore(0)

    def clock(self) -> float:
        return self.now

    def wait(self, seconds: float) -> None:
        self.ticks.acquire(timeout=5)
        self.now += seconds


def make_worker(beat):
    fake_time = FakeTime()
    return chair_land.LandWorker(beat, 30.0, fake_time.clock, fake_time.wait), fake_time


def test_beat_is_called_more_than_once_while_a_land_that_outlasts_the_interval_runs():
    beats: list[float] = []
    twice = threading.Event()
    gate = threading.Event()

    def beat() -> None:
        beats.append(fake_time.now)
        if len(beats) >= 2:
            twice.set()

    worker, fake_time = make_worker(beat)
    submitted = worker.submit("/repo", lambda: gate.wait(5) and "landed")
    assert submitted.outcome == chair_land.Started()
    assert submitted.handle.in_progress()

    fake_time.ticks.release()
    assert twice.wait(5)
    assert submitted.handle.in_progress()
    assert beats[:2] == [0.0, 30.0]

    gate.set()
    assert submitted.handle.wait(5)
    fake_time.ticks.release(10)
    assert submitted.handle.result() == chair_land.LandResult("landed", None, None)


def test_two_lands_for_one_repo_run_one_at_a_time_and_never_overlap():
    worker, fake_time = make_worker(lambda: None)
    log: list[str] = []
    first_gate = threading.Event()

    def first() -> str:
        log.append("first in")
        first_gate.wait(5)
        log.append("first out")
        return "one"

    def second() -> str:
        log.append("second in")
        log.append("second out")
        return "two"

    one = worker.submit("/repo", first)
    two = worker.submit("/repo", second)
    assert (one.outcome, two.outcome) == (chair_land.Started(), chair_land.Queued(1))
    assert two.handle.in_progress()

    first_gate.set()
    assert two.handle.wait(5)
    fake_time.ticks.release(10)

    assert log == ["first in", "first out", "second in", "second out"]
    assert [one.handle.result().value, two.handle.result().value] == ["one", "two"]


def test_a_land_whose_subprocess_takes_the_repo_lease_is_not_refused_by_the_workers_own_hold(monkeypatch):
    """`cox runs land` acquires `land:<repo>` as itself; the worker once held it first and every land came back busy."""
    calls: list[tuple[str, str]] = []

    def acquire(runs_dir, name, holder, ttl, steal=False):
        calls.append((name, holder))
        return store_cli.LeaseRefused(1, "chair-loop") if len(calls) > 1 else store_cli.LeaseGranted(1, holder)

    monkeypatch.setattr(store_cli, "lease_acquire", acquire)
    worker, fake_time = make_worker(lambda: None)
    submitted = worker.submit("/repo", lambda: land_repo_lease.acquire(Path("/runs"), "/repo", "land-subprocess", 1200))
    assert submitted.handle.wait(5)
    fake_time.ticks.release(10)
    assert calls == [("land:/repo", "land-subprocess")]
    assert submitted.handle.result() == chair_land.LandResult(store_cli.LeaseGranted(1, "land-subprocess"), None, None)


def test_stopping_the_worker_refuses_a_queued_land_and_any_later_one():
    worker, fake_time = make_worker(lambda: None)
    gate = threading.Event()
    running = worker.submit("/repo", lambda: gate.wait(5))
    queued = worker.submit("/repo", lambda: "never")
    worker.stop()
    assert queued.handle.wait(5)
    assert isinstance(queued.handle.result().error, chair_land.LandRefused)
    assert worker.submit("/other", lambda: "never") == chair_land.Submitted(chair_land.Refused(None), None)
    gate.set()
    assert running.handle.wait(5)
    fake_time.ticks.release(10)
    assert running.handle.result() == chair_land.LandResult(True, None, None)


def test_a_land_that_raises_anything_is_captured_in_its_result():
    worker, fake_time = make_worker(lambda: None)
    boom = SystemExit(3)

    def land() -> None:
        raise boom

    submitted = worker.submit("/repo", land)
    assert submitted.handle.wait(5)
    fake_time.ticks.release(10)
    assert submitted.handle.result() == chair_land.LandResult(None, boom, None)


def test_may_start_decides_from_plain_data():
    assert chair_land.may_start(0, 1, 0) == chair_land.Started()
    assert chair_land.may_start(1, 1, 2) == chair_land.Queued(3)
    assert chair_land.may_start(0, 1, 0, PAUSED) == chair_land.Queued(1)


def test_may_start_queues_only_once_the_running_count_reaches_the_limit():
    assert chair_land.may_start(1, 2, 0) == chair_land.Started()
    assert chair_land.may_start(2, 2, 0) == chair_land.Queued(1)
    assert chair_land.may_start(1, 2, 1) == chair_land.Queued(2)


def test_a_paused_gate_leaves_a_queued_land_unstarted_across_beats_and_an_unpaused_one_starts_it():
    beats: list[float] = []
    worker, fake_time = make_worker(lambda: beats.append(fake_time.now))
    calls: list[str] = []

    def land() -> str:
        calls.append("landed")
        return "landed"

    submitted = worker.submit("/repo", land, PAUSED)
    for _ in range(3):
        worker.tick(PAUSED)
        fake_time.ticks.release()
    assert submitted.outcome == chair_land.Queued(1)
    assert (calls, beats, fake_time.now) == ([], [], 0.0)
    assert submitted.handle.in_progress()
    assert submitted.handle.result() is None

    worker.tick(chair_land.UNPAUSED)
    assert submitted.handle.wait(5)
    fake_time.ticks.release(10)
    assert calls == ["landed"]
    assert submitted.handle.result() == chair_land.LandResult("landed", None, None)


def test_a_land_already_running_is_not_interrupted_by_a_pause_and_the_land_behind_it_is_held():
    worker, fake_time = make_worker(lambda: None)
    release = threading.Event()
    calls: list[str] = []
    running = worker.submit("/repo", lambda: release.wait(5))
    behind = worker.submit("/repo", lambda: calls.append("behind") or "behind", PAUSED)
    worker.tick(PAUSED)
    assert (running.outcome, behind.outcome) == (chair_land.Started(), chair_land.Queued(1))
    assert running.handle.in_progress()

    release.set()
    assert running.handle.wait(5)
    assert running.handle.result() == chair_land.LandResult(True, None, None)
    assert calls == []
    assert behind.handle.in_progress()

    worker.tick(chair_land.UNPAUSED)
    assert behind.handle.wait(5)
    fake_time.ticks.release(10)
    assert behind.handle.result() == chair_land.LandResult("behind", None, None)


def test_a_fresh_worker_has_no_land_in_flight():
    worker, _ = make_worker(lambda: None)
    assert worker.in_flight() is False


def test_a_submitted_land_blocked_on_an_event_is_in_flight():
    worker, fake_time = make_worker(lambda: None)
    release = threading.Event()
    submitted = worker.submit("/repo", lambda: release.wait(5))
    assert worker.in_flight() is True

    release.set()
    assert submitted.handle.wait(5)
    fake_time.ticks.release(10)


def test_a_land_queued_behind_a_running_one_keeps_the_worker_in_flight():
    worker, fake_time = make_worker(lambda: None)
    release = threading.Event()
    running = worker.submit("/repo", lambda: release.wait(5))
    behind = worker.submit("/repo", lambda: "behind")
    assert behind.outcome == chair_land.Queued(1)
    assert worker.in_flight() is True

    release.set()
    assert running.handle.wait(5) and behind.handle.wait(5)
    fake_time.ticks.release(10)


def test_a_land_held_by_a_paused_gate_is_in_flight():
    worker, fake_time = make_worker(lambda: None)
    held = worker.submit("/repo", lambda: "held", PAUSED)
    assert held.outcome == chair_land.Queued(1)
    assert worker.in_flight() is True

    worker.tick(chair_land.UNPAUSED)
    assert held.handle.wait(5)
    fake_time.ticks.release(10)


def test_in_flight_is_false_once_the_land_finishes_and_its_handle_resolves():
    worker, fake_time = make_worker(lambda: None)
    submitted = worker.submit("/repo", lambda: "landed")
    assert submitted.handle.wait(5)
    fake_time.ticks.release(10)
    # The handle resolves before the worker clears its running count, so poll briefly.
    deadline = time.monotonic() + 5
    while worker.in_flight() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert worker.in_flight() is False


def test_a_stopped_worker_with_nothing_running_is_not_in_flight():
    worker, _ = make_worker(lambda: None)
    worker.stop()
    assert worker.in_flight() is False


def test_two_lands_in_one_repo_with_limit_two_are_both_running_at_once():
    worker, fake_time = make_worker(lambda: None)
    both = threading.Barrier(2, timeout=5)
    release = threading.Event()

    def land() -> str:
        both.wait()
        release.wait(5)
        return "landed"

    one = worker.submit("/repo", land, limit=2)
    two = worker.submit("/repo", land, limit=2)
    assert (one.outcome, two.outcome) == (chair_land.Started(), chair_land.Started())

    release.set()
    assert one.handle.wait(5) and two.handle.wait(5)
    fake_time.ticks.release(10)
    assert [one.handle.result().value, two.handle.result().value] == ["landed", "landed"]


def test_a_third_land_with_limit_two_is_queued_and_starts_when_one_finishes():
    worker, fake_time = make_worker(lambda: None)
    release_one, release_two = threading.Event(), threading.Event()
    running = threading.Semaphore(0)

    def blocked(release: threading.Event) -> str:
        running.release()
        release.wait(5)
        return "landed"

    one = worker.submit("/repo", lambda: blocked(release_one), limit=2)
    two = worker.submit("/repo", lambda: blocked(release_two), limit=2)
    third = worker.submit("/repo", lambda: "third", limit=2)
    assert (one.outcome, two.outcome, third.outcome) == (chair_land.Started(), chair_land.Started(), chair_land.Queued(1))
    assert running.acquire(timeout=5) and running.acquire(timeout=5)
    assert third.handle.in_progress()

    release_one.set()
    assert third.handle.wait(5)
    fake_time.ticks.release(10)
    assert two.handle.in_progress()
    assert third.handle.result() == chair_land.LandResult("third", None, None)
    release_two.set()


def test_limit_one_runs_three_lands_one_after_another():
    worker, fake_time = make_worker(lambda: None)
    log: list[str] = []
    first_gate = threading.Event()

    def first() -> str:
        log.append("first in")
        first_gate.wait(5)
        log.append("first out")
        return "one"

    def later(name: str) -> str:
        log.extend((f"{name} in", f"{name} out"))
        return name

    one = worker.submit("/repo", first, limit=1)
    two = worker.submit("/repo", lambda: later("two"), limit=1)
    three = worker.submit("/repo", lambda: later("three"), limit=1)
    assert (one.outcome, two.outcome, three.outcome) == (chair_land.Started(), chair_land.Queued(1), chair_land.Queued(2))

    first_gate.set()
    assert three.handle.wait(5)
    fake_time.ticks.release(10)
    assert log == ["first in", "first out", "two in", "two out", "three in", "three out"]


def test_a_limit_below_one_is_read_as_one():
    worker, fake_time = make_worker(lambda: None)
    release = threading.Event()
    one = worker.submit("/repo", lambda: release.wait(5), limit=0)
    two = worker.submit("/repo", lambda: "two", limit=-3)
    assert (one.outcome, two.outcome) == (chair_land.Started(), chair_land.Queued(1))
    release.set()
    assert two.handle.wait(5)
    fake_time.ticks.release(10)


def test_a_paused_gate_holds_lands_and_tick_starts_up_to_the_limit_once_unpaused():
    worker, fake_time = make_worker(lambda: None)
    release = threading.Event()
    started = [threading.Event(), threading.Event()]
    calls: list[str] = []

    def blocked(flag: threading.Event) -> str:
        flag.set()
        release.wait(5)
        return "landed"

    one = worker.submit("/repo", lambda: blocked(started[0]), PAUSED, limit=2)
    two = worker.submit("/repo", lambda: blocked(started[1]), PAUSED, limit=2)
    third = worker.submit("/repo", lambda: calls.append("third") or "third", PAUSED, limit=2)
    assert (one.outcome, two.outcome, third.outcome) == (chair_land.Queued(1), chair_land.Queued(2), chair_land.Queued(3))
    worker.tick(PAUSED)
    assert not any(flag.is_set() for flag in started)

    worker.tick(chair_land.UNPAUSED)
    assert all(flag.wait(5) for flag in started)
    assert calls == [] and third.handle.in_progress()

    release.set()
    assert third.handle.wait(5)
    fake_time.ticks.release(10)
    assert third.handle.result() == chair_land.LandResult("third", None, None)


def test_another_repos_running_count_does_not_queue_a_land():
    worker, fake_time = make_worker(lambda: None)
    release = threading.Event()
    busy = worker.submit("/a", lambda: release.wait(5), limit=1)
    behind = worker.submit("/a", lambda: "behind", limit=1)
    other = worker.submit("/b", lambda: "other", limit=1)
    assert (busy.outcome, behind.outcome, other.outcome) == (chair_land.Started(), chair_land.Queued(1), chair_land.Started())
    assert other.handle.wait(5)
    assert busy.handle.in_progress() and behind.handle.in_progress()
    release.set()
    assert behind.handle.wait(5)
    fake_time.ticks.release(10)


def test_beat_due_needs_a_running_land_and_an_elapsed_interval():
    assert chair_land.beat_due(0.0, None, 30.0, 1)
    assert not chair_land.beat_due(0.0, None, 30.0, 0)
    assert not chair_land.beat_due(10.0, 0.0, 30.0, 1)
    assert chair_land.beat_due(30.0, 0.0, 30.0, 1)
