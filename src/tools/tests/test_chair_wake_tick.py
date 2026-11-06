import json
from dataclasses import replace

from test_chair_run import MINE, Rig, _facts

from agent_tools import cli
from agent_tools.chair_plan import plan_tick
from agent_tools.chair_run import run
from agent_tools.chair_wake import wait_for_run_exit

FETCH = {"kind": "fetch_exit", "initiative": "i", "run": "r1"}
INTERVAL = 60
POLL = 5


def _exit(id_):
    return {"id": id_, "ack": False, "event": "run_exited", "fields": {"run": "r1", "initiative": "i", "outcome": "ok"}}


class Loop:
    """The chair loop on fakes: the real planner and wake, a fake exec that records each tick's actions.

    A remote run with a remote record and no fetched marker is the `remote_unfetched` fact the gather returns.
    The clock moves only when the wait or the plain sleep sleeps; the loop stops after `ticks` ticks have waited.
    """

    def __init__(self, ticks, bus=(), arrives_at=None):
        self.t, self.bus, self.arrives_at, self.ticks = 0.0, list(bus), arrives_at, ticks
        self.tick_times, self.actions, self.waits = [], [], []

    def _sleep(self, seconds):
        self.t += seconds
        if self.arrives_at is not None and self.t >= self.arrives_at and not self.bus:
            self.bus.append(_exit("e1"))

    def _gather(self, _deps, _now):
        return {**_facts(MINE), "remote_unfetched": {"i": "r1"}}

    def _perform(self, actions, _exec_deps, _epoch, _dry_run):
        self.tick_times.append(self.t)
        self.actions.append(list(actions))
        return [{"action": a, "status": "done", "reason": ""} for a in actions]

    def _wait(self, interval):
        woke = self.inner(interval)
        self.waits.append((woke, self.t))
        if len(self.waits) >= self.ticks:
            raise KeyboardInterrupt
        return woke

    def run(self):
        self.inner = wait_for_run_exit(lambda: self.bus, self._sleep, lambda: self.t, POLL)
        deps = replace(
            Rig().deps(), gather=self._gather, plan=plan_tick, perform=self._perform, sleep=self._sleep, wait=self._wait
        )
        run(False, INTERVAL, False, deps)
        return self


def _fetches(actions):
    """The fetch_exit actions without the epoch the executor stamps on each."""
    return [{k: a[k] for k in ("kind", "initiative", "run")} for a in actions if a["kind"] == "fetch_exit"]


def test_an_entry_landing_during_the_wait_starts_the_next_tick_early_and_fetches():
    loop = Loop(ticks=2, arrives_at=10).run()
    assert loop.waits[0] == (True, 10)
    assert loop.tick_times == [0, 10] and loop.tick_times[1] < INTERVAL
    assert _fetches(loop.actions[1]) == [FETCH]


def test_with_no_entry_the_wait_runs_the_full_interval_and_the_next_tick_plans_the_same_fetch():
    loop = Loop(ticks=2).run()
    assert loop.waits[0] == (False, INTERVAL)
    assert loop.tick_times == [0, INTERVAL]
    assert _fetches(loop.actions[0]) == [FETCH]
    assert _fetches(loop.actions[1]) == [FETCH]


def test_the_same_entry_does_not_wake_a_second_tick():
    loop = Loop(ticks=3, bus=[_exit("e1")]).run()
    assert loop.waits[:2] == [(True, 0), (False, INTERVAL)]
    assert loop.tick_times == [0, 0, INTERVAL]
    assert all(_fetches(actions) == [FETCH] for actions in loop.actions)


def _deps(tmp_path, dry_run):
    return cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", dry_run, print, tmp_path / "profile.yaml", "files")


def test_the_real_deps_wait_reads_the_workspace_courier_file(tmp_path):
    (tmp_path / "courier.jsonl").write_text(json.dumps(_exit("e1")) + "\n", encoding="utf-8")
    assert _deps(tmp_path, False).wait(INTERVAL) is True


def test_a_dry_run_and_a_missing_workspace_leave_wait_unset(tmp_path):
    assert _deps(tmp_path, True).wait is None
    assert _deps(tmp_path / "gone", False).wait is None
