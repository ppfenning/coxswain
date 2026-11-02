import copy
import os
import signal
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agent_tools import chair_exec, chair_report
from agent_tools.chair_plan import TickPlan, plan_tick
from agent_tools.chair_restart import CheckoutState
from agent_tools.chair_run import (
    DEFAULT_INTERVAL,
    PROFILE_STALE_NOTE,
    RELEASE_HOLD_OFF_SECONDS,
    TAKEOVER_GRACE_SECONDS,
    RunDeps,
    _attempt,
    as_holder,
    error_line,
    land_sink,
    next_gate_record,
    no_landing,
    overran,
    overrun_limit_hit,
    run,
    start_lease_beat,
    tick,
    tick_timed,
)
from agent_tools.ci_gate import CiGate
from agent_tools.forge_status import StatusReader
from agent_tools.notify_core import NotifyConfig
from agent_tools.notify_dispatch import dispatch

NOW = datetime(2026, 9, 26, 18, 5, tzinfo=UTC)  # 14:05 EDT
MINE = {"holder": "me", "host": "box", "epoch": 3, "mine": True, "released": False, "stale": False}
FOREIGN = {**MINE, "holder": "other", "mine": False}
FREE = {**MINE, "mine": False, "released": True}


def _meter_doc(age_minutes):
    return {
        "five_hour": {"used_percentage": 42, "resets_at": 1798650300},
        "seven_day": {"used_percentage": 7, "resets_at": 1799000000},
        "observed_at": (NOW - timedelta(minutes=age_minutes)).isoformat(),
    }


def _facts(lease):
    return {
        "lease": lease,
        "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 2, "go_degraded": False},
        "dispatch": {"max_in_flight": 4, "live_runs": 0, "hosts": []},
        "approved": [
            {
                "id": "t1", "initiative": "i", "repo": "r", "phase": "p", "phase_done": True, "needs": [],
                "run": "x-1", "needs_fetch": False,
            }
        ],
        "initiatives": [],
        "quarantines": [],
        "intake": [],
        "work_store_ready": False,
        "sources_configured": False,
        "last_housekeeping_at": NOW.isoformat(),
        "housekeeping_hours": 24,
    }


class Rig:
    """Fakes for every dep. `lease` is the store row `gather` reads; a losing `beat` overwrites it, as a lost renewal would."""

    def __init__(self, lease=MINE, sleeps_before_interrupt=1, beat_loses=False):
        self.log, self.lines, self.commands, self.acquired, self.sleeps, self.released = [], [], [], [], [], []
        self.recorded = []
        self.lease, self.limit, self.beat_loses, self.holding = lease, sleeps_before_interrupt, beat_loses, True

    def _gather(self, _deps, _now):
        self.log.append("gather")
        return _facts(self.lease)

    def _beat(self):
        self.log.append("beat")
        self.lease = FOREIGN if self.beat_loses else self.lease

    def _sleep(self, seconds):
        self.sleeps.append(seconds)
        if len(self.sleeps) >= self.limit:
            raise KeyboardInterrupt

    def _run(self, argv):
        self.commands.append(argv)
        return 0, "merge: ok mark_done: ok"

    def _acquire(self, holder, host):
        self.acquired.append((holder, host))
        return ""

    def deps(self, echo=None, notify=None) -> RunDeps:
        exec_deps = chair_exec.Deps(
            run=self._run,
            delete_branches=lambda repo, pattern: ([], ""),
            acquire_lease=self._acquire,
            record=self.recorded.append,
            run_id=lambda action: "run-1",
            repo_for=lambda action: "r",
            launch_stagger_s=0.0,  # the stagger decision is pinned in tests/test_chair_exec.py; this rig does not need the real sleep
        )
        return RunDeps(
            facts_deps=object(),
            exec_deps=exec_deps,
            report_deps=chair_report.Deps(echo=echo or self.lines.append, notify=notify),
            beat=self._beat,
            current_epoch=lambda: 3,
            holds=lambda: self.holding,
            release=lambda: self.released.append(True),
            sleep=self._sleep,
            now=lambda: NOW,
            gather=self._gather,
        )


def test_once_runs_one_tick_that_lands_through_the_planner_and_never_sleeps():
    rig = Rig()
    assert run(True, 60, False, rig.deps()) is None
    assert rig.log == ["beat", "gather"]
    assert len(rig.commands) == 1 and "lands 1" in rig.lines[1]
    assert len(rig.lines) == 2 and rig.lines[0].startswith("timing: ") and rig.sleeps == []


def test_a_renewal_lost_in_the_beat_yields_a_standby_line_and_no_land():
    rig = Rig(lease=MINE, beat_loses=True)
    run(True, 60, False, rig.deps())
    assert "standby holder=other host=box" in rig.lines[1]
    assert "lands 0" in rig.lines[1]
    assert rig.commands == []


def test_the_first_tick_acquires_a_free_lease():
    rig = Rig(lease=FREE)
    run(True, 60, False, rig.deps())
    assert rig.acquired == [("", "")]


def test_dry_run_passes_through_and_touches_nothing():
    rig = Rig(lease=FREE)
    run(True, 60, True, rig.deps())
    assert " | dry-run | " in rig.lines[1]
    assert rig.acquired == [] and rig.commands == []


def _dry_run_plan(lease, **over):
    """The action kinds a dry-run tick plans through the real planner, and the rig that performed none of them."""
    rig, planned = Rig(lease=lease), []
    facts = {**_facts(lease), **over}

    def plan(f, n):
        planned.extend(plan_tick(f))
        return planned

    run(True, 60, True, replace(rig.deps(), gather=lambda d, n: facts, plan=plan))
    return [a["kind"] for a in planned], rig


def test_a_dry_run_on_a_released_lease_plans_the_land_the_holder_would():
    kinds, rig = _dry_run_plan(FREE)
    assert kinds == ["land_phase"]
    assert rig.acquired == [] and rig.commands == []


def test_a_dry_run_on_a_stale_lease_plans_the_launch_the_holder_would():
    fresh = {"id": "i", "started": False, "ready_tasks": [{"id": "t", "needs": []}], "landed": set()}
    kinds, rig = _dry_run_plan({**FREE, "released": False, "stale": True}, approved=[], initiatives=[fresh])
    assert kinds == ["launch_epic"]
    assert rig.acquired == [] and rig.commands == []


def test_a_dry_run_behind_a_live_foreign_holder_still_plans_standby():
    kinds, _rig = _dry_run_plan(FOREIGN)
    assert kinds == ["standby"]


def test_a_live_tick_on_a_released_lease_still_plans_take_lease():
    rig, planned = Rig(lease=FREE), []
    run(True, 60, False, replace(rig.deps(), plan=lambda f, n: planned.extend(plan_tick(f)) or planned))
    assert [a["kind"] for a in planned] == ["take_lease"]


def test_as_holder_wins_a_released_or_stale_lease_without_mutating_its_input():
    for lease in (FREE, {**FREE, "released": False, "stale": True}):
        facts = _facts(lease)
        before = copy.deepcopy(facts)
        assert as_holder(facts)["lease"]["mine"] is True
        assert facts == before


def test_as_holder_leaves_a_live_foreign_or_own_lease_as_it_is():
    for lease in (FOREIGN, MINE):
        facts = _facts(lease)
        assert as_holder(facts) == facts


def test_an_exception_in_one_tick_does_not_stop_the_next():
    rig = Rig(sleeps_before_interrupt=2)
    calls = []

    def flaky(deps, now):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("boom")
        return rig._gather(deps, now)

    run(False, DEFAULT_INTERVAL, False, replace(rig.deps(), gather=flaky))
    assert rig.lines[0] == "chair 09-26 14:05 EDT | tick error: ValueError: boom"
    assert "holding" in rig.lines[2]
    assert rig.log == ["beat", "beat", "gather"]
    assert rig.sleeps == [60.0, 60.0]


def test_a_failure_after_perform_names_what_was_performed():
    rig = Rig()
    land = {"kind": "land", "task_id": "t1", "repo": "r", "run": "x-1", "epoch": 3}
    no_dispatch = {k: v for k, v in _facts(MINE).items() if k != "dispatch"}
    run(True, 60, False, replace(rig.deps(), gather=lambda d, n: no_dispatch, plan=lambda f, n: [land]))
    assert len(rig.commands) == 1
    assert rig.lines[1:] == ["chair 09-26 14:05 EDT | tick error: KeyError: 'dispatch' | performed: land:landed"]


def _refused_land_rig(output):
    rig = Rig()
    rig._run = lambda argv: (1, output)
    land = {"kind": "land_phase", "initiative": "ind", "phase": "p1", "repo": "r", "run": "ind-1", "epoch": 3}
    deps = replace(rig.deps(), plan=lambda f, n: [land])
    return rig, deps


def test_a_failed_land_phase_puts_its_reason_in_the_journal_line_and_the_recorded_action():
    rig, deps = _refused_land_rig("land: refusing, working tree is dirty")
    run(True, 60, False, deps)
    assert "land_phase:ind/p1: land: refusing, working tree is dirty" in rig.lines[1]
    recorded = [r for r in rig.recorded if r.get("kind") == "land_phase"]
    assert len(recorded) == 1 and "working tree is dirty" in recorded[0]["reason"]


def test_the_recorded_reason_keeps_only_its_last_600_characters():
    rig, deps = _refused_land_rig("a" * 2000 + "\nthe failing frame")
    run(True, 60, False, deps)
    recorded = next(r for r in rig.recorded if r.get("kind") == "land_phase")
    assert len(recorded["reason"]) <= 600 and recorded["reason"].endswith("the failing frame")
    assert "the failing frame" in rig.lines[1]


def test_a_tick_error_names_the_reason_of_a_failed_result_it_performed():
    failed = {"action": {"kind": "land_phase"}, "status": "not_landed", "reason": "boom\nmerge conflict"}
    landed = {"action": {"kind": "land"}, "status": "landed", "reason": "merge: ok"}
    line = error_line(ValueError("x"), NOW, [failed, landed])
    assert line.endswith("| performed: land_phase:not_landed (merge conflict), land:landed")


def test_a_raising_notify_is_echoed_and_the_next_tick_still_runs():
    rig = Rig(sleeps_before_interrupt=2)

    def notify(_note):
        raise FileNotFoundError("notify-send")

    run(False, 60, False, rig.deps(notify=notify))
    assert rig.lines[2] == "chair 09-26 14:05 EDT | tick error: FileNotFoundError: notify-send"
    assert rig.log == ["beat", "gather", "beat", "gather"]
    assert len(rig.lines) == 6


def test_a_raising_echo_does_not_stop_the_loop():
    rig = Rig(sleeps_before_interrupt=2)

    def echo(_line):
        raise OSError("stdout closed")

    run(False, 60, False, rig.deps(echo=echo))
    assert rig.log == ["beat", "gather", "beat", "gather"]
    assert rig.released == [True]


def test_error_line_is_one_literal_line():
    assert error_line(KeyError("k"), NOW) == "chair 09-26 14:05 EDT | tick error: KeyError: 'k'"


def test_interrupt_releases_the_lease_only_when_held():
    held, not_held = Rig(), Rig()
    not_held.holding = False
    run(False, 60, False, held.deps())
    run(False, 60, False, not_held.deps())
    assert held.released == [True] and not_held.released == []


def test_sigterm_with_a_pending_land_stops_the_worker_and_returns():
    gate = threading.Event()
    lands = land_sink(lambda: None, wait=lambda s: gate.wait(0.01))
    assert lands.submit({"kind": "land", "repo": "/repo"}, lambda: gate.wait(5))["status"] == "in_progress"
    rig = Rig()
    rig._sleep = lambda seconds: os.kill(os.getpid(), signal.SIGTERM)
    assert run(False, 60, False, replace(rig.deps(), stop_lands=lands.stop)) is None
    assert lands.submit({"kind": "land", "repo": "/other"}, lambda: None)["status"] == "busy"
    assert rig.released == [True]
    gate.set()


def test_landing_lists_a_land_in_progress_and_drops_it_once_it_finishes():
    release = threading.Event()
    lands = land_sink(lambda: None, wait=lambda s: release.wait(0.01))
    action = {"kind": "land_phase", "initiative": "ind", "phase": "p1", "repo": "/repo", "run": "ind-1"}
    assert lands.submit(action, lambda: release.wait(5) and {"action": action, "status": "ok", "reason": ""})["status"] == "in_progress"
    assert lands.landing() == [{"initiative": "ind", "phase": "p1", "repo": "/repo"}]
    release.set()
    assert lands._handles["/repo"][0][1].wait(5)
    assert lands.landing() == []
    assert len(lands.collect()) == 1 and lands.landing() == []


def _two_lands_sink(limit, stamp=time.time):
    """A land sink for a repo admitting `limit` lands, the event that releases its blocked lands, and a land action per phase."""
    release = threading.Event()
    sink = land_sink(lambda: None, wait=lambda s: release.wait(0.01), stamp=stamp, limit_for=lambda repo: limit)

    def land(phase):
        return {"kind": "land_phase", "initiative": "ind", "phase": phase, "repo": "/repo", "run": "ind-1", "epoch": 1}

    return release, sink, land


def _submit_blocked(sink, release, action):
    return sink.submit(action, lambda: release.wait(5) and {"action": action, "status": "ok", "reason": ""})


def _until(condition):
    deadline = time.monotonic() + 5
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.005)
    return condition()


def _perform_lands(limit, phases):
    """The statuses, reasons and commands run when `phases` are performed in one tick against a repo with `limit`."""
    release, sink, land = _two_lands_sink(limit)
    rig = Rig()
    exec_deps = replace(rig.deps().exec_deps, lands=sink, run=lambda argv: (rig.commands.append(argv), release.wait(5), (0, "merge: ok"))[2])
    results = chair_exec.perform([land(p) for p in phases], exec_deps, lambda: 1, False)
    _until(lambda: len(rig.commands) == min(limit, len(phases)))
    started = len(rig.commands)
    release.set()
    return [(r["status"], r["reason"]) for r in results], started, len(sink._handles["/repo"])


def test_two_lands_in_a_limit_two_repo_are_both_submitted_and_in_progress():
    results, started, held = _perform_lands(2, ["p1", "p2"])
    assert results == [("in_progress", "landing in /repo")] * 2
    assert started == 2 and held == 2


def test_a_third_land_in_a_limit_two_repo_is_still_running_and_not_submitted():
    results, started, held = _perform_lands(2, ["p1", "p2", "p3"])
    assert results[2] == ("in_progress", "a land in /repo is still running")
    assert started == 2 and held == 2


def test_a_second_land_in_a_limit_one_repo_is_still_running_and_not_submitted():
    results, started, held = _perform_lands(1, ["p1", "p2"])
    assert results == [("in_progress", "landing in /repo"), ("in_progress", "a land in /repo is still running")]
    assert started == 1 and held == 1


def test_full_is_true_only_once_a_repo_holds_as_many_lands_as_its_limit():
    release, sink, land = _two_lands_sink(2)
    assert not sink.pending("/repo") and not sink.full("/repo")
    _submit_blocked(sink, release, land("p1"))
    assert sink.pending("/repo") and not sink.full("/repo")
    _submit_blocked(sink, release, land("p2"))
    assert sink.full("/repo")
    release.set()


def test_collect_returns_both_finished_lands_of_one_repo_and_then_nothing():
    release, sink, land = _two_lands_sink(2)
    _submit_blocked(sink, release, land("p1"))
    _submit_blocked(sink, release, land("p2"))
    release.set()
    assert all(handle.wait(5) for _, handle in sink._handles["/repo"])
    assert len(sink.collect()) == 2
    assert sink.collect() == [] and not sink.pending("/repo")


def test_queued_since_lists_one_start_time_per_running_land_of_one_repo():
    times = iter([10.0, 20.0])
    release, sink, land = _two_lands_sink(2, stamp=lambda: next(times))
    _submit_blocked(sink, release, land("p1"))
    _submit_blocked(sink, release, land("p2"))
    assert _until(lambda: len(sink.queued_since()) == 2)
    assert sorted(sink.queued_since()) == [10.0, 20.0]
    release.set()
    assert _until(lambda: sink.queued_since() == ())


def test_landing_lists_both_lands_in_progress_in_one_repo():
    release, sink, land = _two_lands_sink(2)
    _submit_blocked(sink, release, land("p1"))
    _submit_blocked(sink, release, land("p2"))
    assert sink.landing() == [
        {"initiative": "ind", "phase": "p1", "repo": "/repo"},
        {"initiative": "ind", "phase": "p2", "repo": "/repo"},
    ]
    release.set()


def test_a_chair_with_no_land_worker_reports_no_landing():
    assert no_landing() == []


def test_a_dry_run_installs_no_sigterm_handler():
    installed = []
    rig = Rig(lease=FREE)
    run(True, 60, True, replace(rig.deps(), install_sigterm=lambda: installed.append(True) or (lambda: None)))
    assert installed == []


class Forge:
    """A fake status endpoint and its clock, in epoch seconds."""

    def __init__(self):
        self.at, self.down, self.fetches = NOW.timestamp(), False, 0

    def fetch(self):
        self.fetches += 1
        status = "major_outage" if self.down else "operational"
        return {"components": [{"name": "Actions", "status": status}], "incidents": [{"name": "Actions outage"}]}

    def reader(self):
        return StatusReader(self.fetch, lambda: self.at)

    def tick(self, deps, seconds=60):
        self.at += seconds
        return tick(deps, False, datetime.fromtimestamp(self.at, UTC))


class Performed:
    """A perform that keeps the actions it was given, one list per tick."""

    def __init__(self):
        self.ticks = []

    def __call__(self, actions, _deps, _epoch, _dry_run):
        self.ticks.append([a["kind"] + ":" + a.get("initiative", "") for a in actions])
        return []


def _gated(rig, forge, performed=None, **over):
    """Dependents `dep` and an independent `ind`; a pause drops the relaunch of `dep` and names it held."""
    def plan(_facts, _now):
        return [{"kind": "relaunch", "initiative": "dep", "epoch": 3}, {"kind": "launch_epic", "initiative": "ind", "epoch": 3}]

    def plan_held(_facts, _now, _gate):
        return TickPlan([{"kind": "launch_epic", "initiative": "ind", "epoch": 3}], ("dep",))

    fields = {"status_reader": forge.reader(), "plan": plan, "plan_held": plan_held}
    return replace(rig.deps(), **({"perform": performed} if performed else {}), **fields, **over)


def _needs_chair(rig):
    return [a for a in rig.recorded if a.get("kind") == "needs_chair"]


def test_a_degraded_forge_holds_the_dependent_relaunch_while_an_independent_launch_continues():
    forge, performed = Forge(), Performed()
    deps = _gated(Rig(), forge, performed)
    forge.tick(deps)
    forge.down = True
    forge.tick(deps, 400)
    assert performed.ticks == [["relaunch:dep", "launch_epic:ind"], ["launch_epic:ind", "needs_chair:ci-gate"]]


def test_recovery_on_a_later_tick_resumes_the_relaunch_with_no_extra_record():
    forge, performed = Forge(), Performed()
    deps = _gated(Rig(), forge, performed)
    forge.down = True
    forge.tick(deps)
    forge.down = False
    forge.tick(deps, 400)
    assert performed.ticks[1] == ["relaunch:dep", "launch_epic:ind"]


def test_repeated_degraded_ticks_record_one_needs_chair_naming_the_reason_and_the_held_initiative():
    rig, forge = Rig(), Forge()
    deps = _gated(rig, forge)
    forge.down = True
    for _ in range(3):
        forge.tick(deps, 400)
    [recorded] = _needs_chair(rig)
    assert "Actions outage" in recorded["cause"] and "held for CI: dep" in recorded["cause"]
    assert recorded["status"] == "recorded"
    forge.down = False
    forge.tick(deps, 400)
    assert len(_needs_chair(rig)) == 1
    forge.down = True
    forge.tick(deps, 400)
    assert len(_needs_chair(rig)) == 2


def test_a_degraded_tick_holds_the_land_and_makes_no_merge_until_the_forge_recovers():
    rig, forge = Rig(), Forge()
    ev = threading.Event()
    sink = land_sink(lambda: None, wait=lambda s: ev.wait(0.01))
    deps = replace(
        rig.deps(), status_reader=forge.reader(), set_gate=sink.set_gate, queued_since=sink.queued_since,
        exec_deps=replace(rig.deps().exec_deps, lands=sink),
    )
    forge.down = True
    forge.tick(deps)
    assert rig.commands == [] and sink.pending("r")
    forge.down = False
    forge.tick(deps, 400)
    sink._handles["r"][0][1].wait(5)
    assert len(rig.commands) == 1
    assert len(_needs_chair(rig)) == 1


def test_a_land_queued_past_the_bound_pauses_an_operational_forge_and_resumes_when_its_checks_start():
    rig, forge, queued = Rig(), Forge(), [NOW.timestamp() - 1801 + 60]
    performed = Performed()
    deps = _gated(rig, forge, performed, queued_since=lambda: tuple(queued))
    forge.tick(deps)
    queued.clear()
    forge.tick(deps, 10)
    assert performed.ticks[0] == ["launch_epic:ind", "needs_chair:ci-gate"]
    assert performed.ticks[1] == ["relaunch:dep", "launch_epic:ind"]


def test_the_endpoint_is_fetched_once_across_ticks_inside_five_minutes():
    forge = Forge()
    deps = _gated(Rig(), forge, Performed())
    forge.tick(deps)
    forge.tick(deps)
    forge.tick(deps, 120)
    assert forge.fetches == 1
    forge.tick(deps, 301)
    assert forge.fetches == 2


def test_a_gate_records_only_on_the_tick_that_pauses_it():
    paused = CiGate(True, "outage")
    assert next_gate_record(False, paused, ("a", "b"), 3) == {
        "kind": "needs_chair", "initiative": "ci-gate", "cause": "ci_gate_paused: outage; held for CI: a, b", "epoch": 3,
    }
    assert next_gate_record(True, paused, ("a",), 3) is None
    assert next_gate_record(True, CiGate(False, ""), (), 3) is None


def test_a_dry_run_records_no_gate_and_leaves_the_carried_state_alone():
    rig, forge = Rig(lease=FREE), Forge()
    deps = _gated(rig, forge)
    forge.down = True
    forge.at += 60
    tick(deps, True, datetime.fromtimestamp(forge.at, UTC))
    assert _needs_chair(rig) == [] and deps.gate_state.paused is False


@pytest.mark.parametrize("gated", [False, True])
def test_an_unpaused_tick_reads_no_lease_from_the_facts_and_keeps_the_carried_state(gated):
    forge, seen = Forge(), []
    deps = replace(
        Rig().deps(), gather=lambda _d, _n: {}, plan=lambda f, n: seen.append(f) or [],
        perform=lambda *_: [], **({"status_reader": forge.reader()} if gated else {}),
    )
    forge.tick(deps)
    assert seen == [{}] and deps.gate_state.paused is False


def test_each_chair_carries_its_own_gate_state():
    assert Rig().deps().gate_state is not Rig().deps().gate_state


def _meter_actions(rig):
    return [a for a in rig.recorded if a.get("kind") == "meter"]


def test_a_fresh_meter_reading_is_recorded_once_with_the_docs_fields():
    rig, doc = Rig(), _meter_doc(5)
    tick(replace(rig.deps(), meter_doc=lambda: doc), False, NOW)
    (action,) = _meter_actions(rig)
    assert action["status"] == "recorded"
    assert (action["five_hour"], action["seven_day"], action["observed_at"]) == (
        doc["five_hour"], doc["seven_day"], doc["observed_at"],
    )


@pytest.mark.parametrize("reading", [None, _meter_doc(16)], ids=["missing", "16-minutes-old"])
def test_a_missing_or_stale_meter_reading_records_no_meter_action(reading):
    rig = Rig()
    tick(replace(rig.deps(), meter_doc=lambda: reading), False, NOW)
    assert _meter_actions(rig) == []


def test_a_dry_run_status_line_says_remote_lanes_were_not_probed_and_a_live_one_does_not():
    assert tick(Rig().deps(), True, NOW).endswith(f" | {chair_exec.NOT_PROBED}")
    assert chair_exec.NOT_PROBED not in tick(Rig().deps(), False, NOW)


def test_a_stale_profile_fact_adds_the_note_to_the_status_line_and_its_absence_does_not():
    rig = Rig()
    stale = replace(rig.deps(), gather=lambda d, n: {**_facts(MINE), "profile_stale": True})
    assert tick(stale, False, NOW).endswith(f" | {PROFILE_STALE_NOTE}")
    assert PROFILE_STALE_NOTE not in tick(Rig().deps(), False, NOW)


def test_the_written_status_line_carries_the_stale_profile_note():
    rig = Rig()
    run(True, 60, False, replace(rig.deps(), gather=lambda d, n: {**_facts(MINE), "profile_stale": True}))
    assert "profile re-read failed; keeping the last good profile" in rig.lines[-1]


def test_a_dry_run_records_no_meter_action_even_when_fresh():
    rig = Rig()
    tick(replace(rig.deps(), meter_doc=lambda: _meter_doc(5)), True, NOW)
    assert _meter_actions(rig) == []


def test_a_gather_that_raises_still_publishes_the_meter_once():
    rig = Rig()

    def boom(_deps, _now):
        raise RuntimeError("gather down")

    deps = replace(rig.deps(), gather=boom, meter_doc=lambda: _meter_doc(5))
    with pytest.raises(RuntimeError):
        tick(deps, False, NOW)
    assert len(_meter_actions(rig)) == 1


def test_overran_is_true_at_and_past_the_bound_only():
    assert (overran(0.1, 0.2), overran(0.2, 0.2), overran(0.3, 0.2)) == (False, True, True)


def _hanging(rig, release, calls):
    def gather(deps, now):
        calls.append(1)
        release.wait(30)
        return rig._gather(deps, now)

    return replace(rig.deps(), gather=gather, facts_bound=0.2)


def test_a_gather_that_never_returns_is_abandoned_and_the_overrun_is_counted():
    rig, release, calls = Rig(), threading.Event(), []
    deps = _hanging(rig, release, calls)
    planned = []
    deps = replace(deps, plan=lambda f, n: planned.append(1) or [], perform=lambda *a: planned.append(1) or [])
    try:
        started = time.monotonic()
        _attempt(deps, False)
        assert time.monotonic() - started < 1.0
        assert "overrun: facts stage over 0.2s (1 in a row)" in rig.lines[-1]
        _attempt(deps, False)
        assert "overrun: facts stage over 0.2s (2 in a row)" in rig.lines[-1]
        assert (len(calls), deps.overrun_state.consecutive, planned) == (2, 2, [])
    finally:
        release.set()


def test_a_tick_that_completes_resets_the_overrun_count():
    rig, release, calls = Rig(), threading.Event(), []
    hanging = _hanging(rig, release, calls)
    try:
        _attempt(hanging, False)
        assert hanging.overrun_state.consecutive == 1
        _attempt(replace(hanging, gather=rig._gather), False)
        assert hanging.overrun_state.consecutive == 0
    finally:
        release.set()


def test_overrun_limit_hit_is_true_at_and_past_a_positive_limit_only():
    cases = [(0, 0), (99, 0), (2, 3), (3, 3), (4, 3), (1, -1)]
    assert [overrun_limit_hit(c, n) for c, n in cases] == [False, False, False, True, True, False]


def _scripted(rig, hangs, release, calls):
    """A gather that hangs past the bound on every tick whose entry in `hangs` is true."""

    def gather(deps, now):
        calls.append(1)
        if hangs[(len(calls) - 1) % len(hangs)]:
            release.wait(30)
        return rig._gather(deps, now)

    return replace(rig.deps(), gather=gather, facts_bound=0.05)


def test_overruns_every_tick_stop_the_loop_after_exactly_the_limit_and_free_the_lease():
    rig, release, calls = Rig(sleeps_before_interrupt=100), threading.Event(), []
    stopped = []
    deps = replace(_scripted(rig, [True], release, calls), stop_lands=lambda: stopped.append(True))
    try:
        assert run(False, 60, False, deps, 3) is None
    finally:
        release.set()
    assert (len(calls), len(rig.sleeps), stopped, rig.released) == (3, 2, [True], [True])


def test_overruns_broken_by_a_completed_tick_never_stop_the_loop():
    rig, release, calls = Rig(sleeps_before_interrupt=8), threading.Event(), []
    deps = _scripted(rig, [True, True, False], release, calls)
    try:
        run(False, 60, False, deps, 3)
    finally:
        release.set()
    assert (len(calls), len(rig.sleeps)) == (8, 8)  # only the interrupt in the eighth sleep ended it


def test_a_limit_of_zero_never_stops_on_overruns():
    rig, release, calls = Rig(sleeps_before_interrupt=5), threading.Event(), []
    deps = _scripted(rig, [True], release, calls)
    try:
        run(False, 60, False, deps, 0)
    finally:
        release.set()
    assert (len(calls), deps.overrun_state.consecutive) == (5, 5)


OLD, NEW = "a" * 40, "b" * 40
ON_OLD = CheckoutState(OLD, True)


def _checkout(rig, states, in_flight=None, started=ON_OLD, **over):
    """Deps whose checkout read pops `states` in turn (the last one repeats); `reads` counts them."""
    reads = []

    def read():
        reads.append(1)
        state = states[min(len(reads) - 1, len(states) - 1)]
        if isinstance(state, Exception):
            raise state
        return state

    flight = in_flight or []
    lands = lambda: flight.pop(0) if len(flight) > 1 else (flight[0] if flight else False)  # noqa: E731
    deps = replace(rig.deps(), checkout_state=read, started_checkout=started, lands_in_flight=lands, **over)
    return deps, reads


def _restart_lines(rig):
    return [line for line in rig.lines if "restarting" in line]


def test_a_moved_head_on_a_branch_ends_the_loop_after_the_tick_that_finished_and_frees_the_lease():
    rig = Rig(sleeps_before_interrupt=100)
    deps, reads = _checkout(rig, [CheckoutState(NEW, True)])
    assert run(False, 60, False, deps) is None
    assert (rig.log.count("gather"), len(rig.sleeps), len(reads), rig.released) == (1, 1, 1, [True])
    assert deps.restart_state.moved == (OLD, NEW)
    assert _restart_lines(rig) == [f"chair restarting because the checkout moved from {OLD[:12]} to {NEW[:12]}"]


def test_an_unmoved_head_keeps_ticking_and_never_sets_moved():
    rig = Rig(sleeps_before_interrupt=4)
    deps, reads = _checkout(rig, [CheckoutState(OLD, True)])
    run(False, 60, False, deps)
    assert (rig.log.count("gather"), len(reads), deps.restart_state.moved, _restart_lines(rig)) == (4, 3, None, [])


def test_a_detached_start_never_ends_the_loop_even_when_the_head_differs():
    rig = Rig(sleeps_before_interrupt=3)
    deps, _ = _checkout(rig, [CheckoutState(NEW, True)], started=CheckoutState(OLD, False))
    run(False, 60, False, deps)
    assert (rig.log.count("gather"), deps.restart_state.moved) == (3, None)


def test_a_moved_head_with_a_land_in_flight_ticks_again_and_ends_on_the_first_tick_after_it_finishes():
    rig = Rig(sleeps_before_interrupt=100)
    deps, reads = _checkout(rig, [CheckoutState(NEW, True)], in_flight=[True, True, False])
    run(False, 60, False, deps)
    assert (rig.log.count("gather"), len(reads), deps.restart_state.moved, rig.released) == (3, 3, (OLD, NEW), [True])


def test_once_and_dry_run_never_consult_the_checkout():
    for once, dry in ((True, False), (False, True)):
        rig = Rig(sleeps_before_interrupt=2)
        deps, reads = _checkout(rig, [CheckoutState(NEW, True)])
        run(once, 60, dry, deps)
        assert (reads, deps.restart_state.moved) == ([], None)


@pytest.mark.parametrize("state", [None, RuntimeError("git gone")])
def test_an_unreadable_checkout_ticks_as_usual(state):
    rig = Rig(sleeps_before_interrupt=3)
    deps, reads = _checkout(rig, [state])
    run(False, 60, False, deps)
    assert (rig.log.count("gather"), len(reads), deps.restart_state.moved) == (3, 2, None)


def test_a_gather_that_raises_inside_the_bound_still_surfaces_its_exception():
    rig = Rig()

    def boom(_deps, _now):
        raise RuntimeError("gather down")

    deps = replace(rig.deps(), gather=boom)
    with pytest.raises(RuntimeError, match="gather down"):
        tick(deps, False, NOW)
    assert deps.overrun_state.consecutive == 0


def test_a_tick_records_one_status_row_with_its_line():
    rig = Rig()
    run(True, 60, False, rig.deps())
    (row,) = [a for a in rig.recorded if a.get("kind") == "status"]
    assert row["line"] == rig.lines[1] and row["status"] == "recorded"


def test_a_dry_run_records_no_status_row():
    rig = Rig(lease=FREE)
    run(True, 60, True, rig.deps())
    assert [a for a in rig.recorded if a.get("kind") == "status"] == []


def test_a_failing_meter_read_or_record_cannot_break_the_tick():
    rig = Rig()

    def read_fails():
        raise OSError("gone")

    tick(replace(rig.deps(), meter_doc=read_fails), False, NOW)
    assert rig.log == ["beat", "gather"]


STRANDED = {"kind": "needs_chair", "initiative": "ind", "cause": "stranded", "reason": "no live run"}
PUSH = NotifyConfig("https://ntfy.sh/t")


def _notify_deps(rig, tmp_path, notify, config=PUSH):
    return replace(
        rig.deps(), gather=lambda d, n: _facts(MINE), plan=lambda f, n: [STRANDED], perform=lambda a, d, e, dry: [],
        notify=notify, notify_config=config, workspace=tmp_path,
    )


def test_a_tick_hands_its_needs_chair_event_to_notify_with_the_config_state_path_and_now(tmp_path):
    calls = []
    deps = _notify_deps(Rig(), tmp_path, lambda *args: calls.append(args) or [])
    tick(deps, False, NOW)
    [(events, config, state_path, stamp)] = calls
    assert [e.key for e in events] == ["needs_chair:ind:stranded"]
    assert config == NotifyConfig("https://ntfy.sh/t")
    assert state_path == tmp_path / "notify-sent.json" and stamp == NOW.timestamp()


def test_a_notify_that_raises_is_written_as_a_status_line_and_the_next_tick_still_runs(tmp_path):
    calls = []

    def boom(*args):
        calls.append(args)
        raise OSError("ntfy down")

    rig = Rig(sleeps_before_interrupt=2)
    run(False, 60, False, _notify_deps(rig, tmp_path, boom))
    assert len(calls) == 2 and len(rig.lines) == 4
    assert all("notify failed: OSError: ntfy down" in line for line in rig.lines[1::2])


def test_a_dry_run_never_calls_notify(tmp_path):
    calls = []
    tick(_notify_deps(Rig(), tmp_path, lambda *args: calls.append(args) or []), True, NOW)
    assert calls == []


def test_a_profile_with_no_notify_config_dispatches_none_and_sends_nothing(tmp_path):
    seen, posts = [], []

    def through_dispatch(events, config, path, now):
        seen.append(config)
        return dispatch(events, config, path, now, post=lambda *a, **k: posts.append(a) or True)

    tick(_notify_deps(Rig(), tmp_path, through_dispatch, config=None), False, NOW)
    assert seen == [None] and posts == []
    assert not (tmp_path / "notify-sent.json").exists()


WAITING_PART = "waiting: a (blocked by p1: 1 quarantined, 0 awaiting land)"


def _blocked_rig(reported=None, hard_stop=False, blocked=True):
    """A rig whose facts hold initiative `a` with a ready p2 task; a p1 row with one quarantined task blocks it."""
    rig = Rig()
    initiative = {
        "id": "a", "repo": "repo-a", "started": True,
        "ready_tasks": [{"id": "a-t", "phase": "p2", "needs": []}], "landed": set(),
    }
    rows = [{"initiative": "a", "phase": "p1", "tasks": [("a-q", "quarantined")]}] if blocked else []

    def gather(_deps, _now):
        facts = _facts(rig.lease)
        return {
            **facts,
            "limits": {**facts["limits"], "hard_stop": hard_stop},
            "initiatives": [initiative],
            "phase_tasks": rows,
            "run_exited": {"a": True},
            "waiting_reported": reported or {},
        }

    return rig, replace(rig.deps(), gather=gather)


def test_a_blocked_initiative_shows_on_the_status_line():
    _, deps = _blocked_rig()
    assert WAITING_PART in tick(deps, False, NOW)


def test_a_deduplicated_waiting_record_still_shows_on_the_status_line():
    rig, deps = _blocked_rig(reported={"a": "p1|1|0"})
    assert WAITING_PART in tick(deps, False, NOW)
    assert [a for a in rig.recorded if a.get("kind") == "waiting"] == []


def test_a_blocked_initiative_shows_on_the_status_line_under_the_hard_stop():
    _, deps = _blocked_rig(hard_stop=True)
    assert WAITING_PART in tick(deps, False, NOW)


def test_a_blocked_initiative_shows_on_a_dry_run_status_line():
    _, deps = _blocked_rig()
    assert WAITING_PART in tick(deps, True, NOW)


def test_no_blocked_initiative_leaves_no_waiting_part():
    _, deps = _blocked_rig(blocked=False)
    assert "waiting:" not in tick(deps, False, NOW)


RELEASED = "released at takeover request"


class Takeover:
    """A fake clock and a release check that answers from `asked` and counts how often it is asked."""

    def __init__(self, asked=True):
        self.asked, self.checks, self.seconds = asked, 0, 1000.0

    def clock(self):
        return self.seconds

    def check(self):
        self.checks += 1
        return self.asked


def _released_deps(rig, takeover, **over):
    calls = []

    def plan(*args):
        calls.append("plan")
        return plan_tick(*args)

    def perform(*args):
        calls.append("perform")
        return []

    deps = replace(rig.deps(), plan=plan, perform=perform, release_requested=takeover.check, **over)
    return deps, calls


def test_a_tick_asked_to_release_does_nothing_else_and_returns_the_release_line():
    rig, takeover = Rig(), Takeover()
    deps, calls = _released_deps(rig, takeover)
    line, stages, sources = tick_timed(deps, False, NOW, takeover.clock)
    assert line == f"chair 09-26 14:05 EDT | {RELEASED}"
    assert (stages, sources) == ({}, {})
    assert rig.log == [] and calls == [] and rig.commands == []


@pytest.mark.parametrize("asked", [False, None])
def test_a_false_or_missing_release_check_ticks_as_before(asked):
    rig, takeover = Rig(), Takeover(asked=asked)
    deps, _ = _released_deps(rig, takeover)
    deps = deps if asked is False else replace(deps, release_requested=None)
    line, _, _ = tick_timed(deps, False, NOW, takeover.clock)
    assert rig.log == ["beat", "gather"] and RELEASED not in line


def test_a_release_sets_the_beat_threads_stop_event_and_a_false_check_leaves_it_clear():
    rig, takeover = Rig(), Takeover(asked=False)
    deps, _ = _released_deps(rig, takeover)
    stop = start_lease_beat(deps, True)
    assert deps.release_state.beating is stop
    tick_timed(deps, False, NOW, takeover.clock)
    assert not stop.is_set()
    takeover.asked = True
    tick_timed(deps, False, NOW, takeover.clock)
    assert stop.is_set()


def test_run_hands_the_beat_threads_own_event_to_the_release():
    rig, takeover = Rig(), Takeover()
    deps, _ = _released_deps(rig, takeover, lease_beat=lambda: "", lease_beat_interval=0.01)
    run(True, 60, False, deps)
    assert deps.release_state.beating is not None and deps.release_state.beating.is_set()
    assert rig.log == [] and RELEASED in rig.lines[-1]


def test_ticks_inside_the_hold_off_window_stand_by_and_one_after_it_beats():
    rig, takeover = Rig(), Takeover()
    deps, _ = _released_deps(rig, takeover)
    tick_timed(deps, False, NOW, takeover.clock)
    takeover.asked = False
    takeover.seconds += RELEASE_HOLD_OFF_SECONDS - 1
    line, stages, _ = tick_timed(deps, False, NOW, takeover.clock)
    assert "standby: released at takeover request" in line and stages == {}
    assert rig.log == [] and takeover.checks == 1
    takeover.seconds += 1
    tick_timed(deps, False, NOW, takeover.clock)
    assert rig.log == ["beat", "gather"] and takeover.checks == 2


def test_the_hold_off_window_is_at_least_twice_the_takeover_grace():
    assert RELEASE_HOLD_OFF_SECONDS >= 2 * TAKEOVER_GRACE_SECONDS == 40.0


def test_a_dry_run_never_asks_whether_a_release_was_requested():
    rig, takeover = Rig(), Takeover()
    deps, _ = _released_deps(rig, takeover)
    line, _, _ = tick_timed(deps, True, NOW, takeover.clock)
    assert takeover.checks == 0 and RELEASED not in line
