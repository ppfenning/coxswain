from dataclasses import replace

from test_chair_run import Rig

from agent_tools.chair_run import run
from agent_tools.chair_wake import new_run_exits, wait_for_run_exit


def _exit(id_, **over):
    return {"id": id_, "ack": False, "event": "run_exited", "fields": {"run": "r1", "initiative": "i", "outcome": "ok"}, **over}


class Clock:
    """A fake clock whose sleep advances it and records each step."""

    def __init__(self):
        self.t, self.steps = 0.0, []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.steps.append(seconds)
        self.t += seconds


def test_new_run_exits_skips_acked_other_events_malformed_and_seen_ids():
    entries = [
        _exit("a", ack=True),
        _exit("b", event="stuck"),
        _exit("c", fields={"run": "r1"}),
        _exit("d"),
        _exit("e"),
    ]
    assert new_run_exits(entries, {"d"}) == [entries[4]]


def test_the_wait_returns_early_on_an_event_and_one_entry_wakes_it_once():
    clock, bus = Clock(), []
    wait = wait_for_run_exit(lambda: bus, clock.sleep, clock.now, 5)
    bus.append(_exit("a"))
    assert wait(60) is True and clock.steps == []
    assert wait(60) is False and clock.t == 60


def test_an_event_arriving_mid_wait_ends_it_at_the_next_poll():
    clock, bus = Clock(), []

    def sleep(seconds):
        clock.sleep(seconds)
        if clock.t >= 10:
            bus.append(_exit("a"))

    wait = wait_for_run_exit(lambda: bus, sleep, clock.now, 5)
    assert wait(60) is True and clock.t == 10


def test_the_wait_returns_false_after_the_full_interval_when_no_event_arrives():
    clock = Clock()
    wait = wait_for_run_exit(lambda: [_exit("x", ack=True)], clock.sleep, clock.now, 25)
    assert wait(60) is False
    assert clock.steps == [25, 25, 10]


def test_a_failing_fetch_after_a_wake_neither_wakes_again_nor_raises():
    clock, calls = Clock(), []

    def read():
        calls.append(1)
        if len(calls) > 1:
            raise OSError("bus down")
        return [_exit("a")]

    wait = wait_for_run_exit(read, clock.sleep, clock.now, 30)
    assert wait(60) is True
    assert wait(60) is False and clock.t == 60


def test_run_sleeps_when_wait_is_none_and_waits_when_it_is_set():
    plain = Rig(sleeps_before_interrupt=1)
    run(False, 60, False, plain.deps())
    assert plain.sleeps == [60]

    waited, with_wait = [], Rig()

    def wait(interval):
        waited.append(interval)
        raise KeyboardInterrupt

    run(False, 60, False, replace(with_wait.deps(), wait=wait))
    assert waited == [60] and with_wait.sleeps == []
