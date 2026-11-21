"""C1: the holder's tick is planned per kind of work, and plans the same actions, in the same order, as before the split.

`plan_equivalence.legacy_plan_as_holder` is `chair_plan._plan_as_holder` as it stood before the split. Each scenario
runs both on the same facts, with and without the tick's clock and a CI-gate hold. Several scenarios reuse the facts of
the focused test modules that prove the branch fires; `test_each_scenario_reaches_its_branch` checks it still does.
Delete the scenarios' legacy comparison when the queue workers (C3-C5) replace the tick; the per-kind tests stay.
"""

import copy
import dataclasses
from datetime import UTC, datetime

import pytest

from agent_tools.chair_plan import (
    LaunchPlan,
    TickBasis,
    _plan_as_holder,
    launch_gates,
    plan_decompose_kind,
    plan_housekeeping_kind,
    plan_land_kind,
    plan_launch_kind,
    plan_quarantine_kind,
    plan_watch_kind,
    tick_basis,
)
from agent_tools.chair_types import Facts
from tests.plan_equivalence import legacy_plan_as_holder
from tests.test_chair_plan_carry import _BRANCH, _RELAUNCHING, _STRANDED
from tests.test_chair_plan_deadlock_line import _NOW as _DEADLOCK_NOW
from tests.test_chair_plan_deadlock_line import _deadlock_facts
from tests.test_chair_plan_lease_refusal import _NOW as _LEASE_NOW
from tests.test_chair_plan_lease_refusal import _alive, _streak
from tests.test_chair_plan_lease_refusal import _facts as _lease_facts
from tests.test_chair_plan_steer import _NOW as _STEER_NOW
from tests.test_chair_plan_steer import _facts as _steer_facts
from tests.test_chair_plan_steer import _initiative as _steer_initiative
from tests.test_chair_plan_steer_cycle import _LIMITS as _CYCLE_LIMITS
from tests.test_chair_plan_steer_cycle import _NOW as _CYCLE_NOW
from tests.test_chair_plan_steer_cycle import _cycle
from tests.test_chair_plan_steer_cycle import _idle as _cycle_idle
from tests.test_chair_schedule_launch import NOW as _SCHEDULE_NOW
from tests.test_chair_schedule_launch import _facts as _schedule_facts
from tests.test_chair_stall_alarm import _NOW as _ALARM_NOW
from tests.test_chair_stall_alarm import _OPEN, _STREAK, _ts
from tests.test_chair_stall_alarm import _facts as _alarm_facts

_NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)

_LIMITS = {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False}
_RED_MAIN = {
    "initiative": "i", "phase": "p", "repo": "r", "pr": 7, "commit": "abc", "ci": "red", "ci_output": "2 failed",
    "smoke": "pending", "smoke_output": "",
}


def _facts(**overrides: object) -> Facts:
    base = {
        "lease": {"holder": "a", "host": "h", "epoch": 7, "mine": True, "released": False, "stale": False},
        "limits": _LIMITS,
        "dispatch": {"max_in_flight": 5, "live_runs": 0, "hosts": []},
        "approved": [],
        "initiatives": [],
        "quarantines": [],
        "intake": [],
        "work_store_ready": True,
        "sources_configured": True,
        "remote_unfetched": {},
        "run_exited": {},
        "schema_deaths": {},
        "lost_runs": {},
        "last_housekeeping_at": None,
        "housekeeping_hours": 24.0,
        "stale_days": 7,
        "stale_candidates": [],
        "stall_candidates": [],
        "home": {},
    }
    return {**base, **overrides}  # type: ignore[return-value]


def _started(id: str, needs: tuple[str, ...] = ()) -> dict:
    return {"id": id, "started": True, "ready_tasks": [{"id": f"{id}-t", "needs": list(needs)}], "landed": set()}


def _unstarted(id: str) -> dict:
    return {**_started(id), "started": False}


def _approved(id: str, initiative: str = "x") -> dict:
    return {
        "id": id, "initiative": initiative, "repo": "r", "phase": "p", "phase_done": True, "needs": [], "run": f"{initiative}-1",
        "needs_fetch": False,
    }


def _stale_row(initiative: str, task_id: str) -> dict:
    return {
        "initiative": initiative, "task_id": task_id, "state": "ready", "last_file_change": "2026-09-01T00:00:00+00:00",
        "last_run": None, "last_chair_action": None, "quarantine_non_harness_count": 0,
    }


_STALL = {
    "run": "r1", "initiative": "s", "local": True, "started_at": "2026-09-27T11:00:00Z",
    "last_call": {"role": "builder", "task": "t1", "ts": "2026-09-27T11:29:00Z"}, "usr1_sent": False,
}

def _deadlock_with_lease_refusal() -> Facts:
    """`a` waits on a quarantined phase (`waiting`); `b`'s launch was refused on a lease its live holder still keeps."""
    facts = _deadlock_facts()
    b = {"id": "b", "repo": "r-b", "started": True, "ready_tasks": [{"id": "b-t", "needs": []}], "landed": set()}
    return {  # type: ignore[return-value]
        **facts,
        "initiatives": [*facts["initiatives"], b],
        "run_exited": {"a": True, "b": True},
        "lease_refusals": {"b": {"first_at": "2026-11-04T11:59:00+00:00", "last_at": "2026-11-04T11:59:30+00:00", "holder_run": "h1"}},
        "running": [{"id": "h1", "repo": "r-h1", "surfaces": []}],
    }


_ACCESSOR_STOP = "Needs a getter in src/app.rs for the focused frame."
_HALTED = {"max_in_flight": 0, "live_runs": 0, "hosts": []}

# Each scenario is facts and the clock its builder's dates assume. Together they reach every kind's planner, both
# branches of the limits gate, and each slot of the tick's layout that the facts can reach today.
_SCENARIOS: dict[str, tuple[Facts, datetime]] = {
    "empty": (_facts(), _NOW),
    "launch fills free lanes": (_facts(initiatives=[_unstarted("a"), _unstarted("b"), _started("c")], run_exited={"c": True}), _NOW),
    "launch cap binds": (
        _facts(limits={**_LIMITS, "launch_cap": 1}, initiatives=[_started("a"), _started("b"), _unstarted("c")], run_exited={"a": True, "b": True}),
        _NOW,
    ),
    "quarantine": (
        _facts(
            initiatives=[_unstarted("q"), _started("w", ("z",)), _unstarted("u")],
            quarantines=[
                {"task_id": "q-t", "initiative": "q", "cause": "scope", "harness_failures": 0},
                {"task_id": "k-t", "initiative": "k", "cause": "harness", "harness_failures": 1, "has_patch": False, "rescue_failed": False},
            ],
        ),
        _NOW,
    ),
    # C4: the triage a quarantine worker makes, beside a retry (which starts a run, so it stays the launch's) and a
    # decompose launch from a new intake (the decompose worker's).
    "triage, a hold and a retry beside a decompose": (
        _facts(
            initiatives=[_started("q"), _started("h")],
            run_exited={"q": True, "h": True},
            quarantines=[
                {"task_id": "q-t", "initiative": "q", "cause": "code", "harness_failures": 0, "run": "q-3", "failures": 1},
                {"task_id": "h-t", "initiative": "h", "cause": "review", "harness_failures": 0, "run": "h-2", "failures": 5},
                {"task_id": "k-t", "initiative": "k", "cause": "harness", "harness_failures": 1, "has_patch": False, "rescue_failed": False},
            ],
            intake=["n1"],
        ),
        _NOW,
    ),
    "land and watch": (_facts(approved=[_approved("t1"), _approved("t2", "y")], landed_main=[_RED_MAIN], initiatives=[_started("x")]), _NOW),
    "hard stop": (
        _facts(
            limits={**_LIMITS, "hard_stop": True, "weekly_fraction": 0.95},
            approved=[_approved("t1")],
            initiatives=[_started("i"), _unstarted("j")],
            quarantines=[{"task_id": "p", "initiative": "m", "cause": "scope", "harness_failures": 0}],
            intake=["n1"],
            landed_main=[_RED_MAIN],
        ),
        _NOW,
    ),
    "housekeeping ages": (
        _facts(stale_candidates=[_stale_row("a", "a-1"), _stale_row("b", "b-1")], stall_candidates=[_STALL], initiatives=[_started("a")], run_exited={"a": True}),
        _NOW,
    ),
    "decompose": (_facts(empty_decompose=[{"initiative": "m", "run": "m-1"}], intake=["n1", "n2"]), _NOW),
    "lost and unfetched runs": (
        _facts(
            initiatives=[_started("l"), _started("f"), _started("e")],
            lost_runs={"l": "l-2"},
            remote_unfetched={"f": "f-3"},
            run_hosts={"f-3": "lane-1"},
            run_exited={"e": False},
        ),
        _NOW,
    ),
    "waiting, idle stall and a held lease": (_deadlock_with_lease_refusal(), _DEADLOCK_NOW),
    "a lease stuck past its ttl": (_lease_facts(lease_refusals=_streak(7200, 60), running=_alive()), _LEASE_NOW),
    "gated initiatives": (
        _facts(
            held_initiatives=["a"],
            deferred_initiatives={"b": "v2"},
            initiatives=[_started("a"), _unstarted("b"), _unstarted("c")],
            approved=[_approved("t1", "a"), _approved("t2", "c")],
            run_exited={"a": True},
        ),
        _NOW,
    ),
    "steer cycle and approval restores": (_cycle(wrongly_released=[{"initiative": "A", "task_id": "A-t"}]), _CYCLE_NOW),
    "idle ready with no lane": (_cycle(dispatch=_HALTED, idle_stall=_cycle_idle(31)), _CYCLE_NOW),
    "idle ready at the hard stop": (_cycle(limits={**_CYCLE_LIMITS, "hard_stop": True}, idle_stall=_cycle_idle(31)), _CYCLE_NOW),
    "carries and rebases": (_facts(**_RELAUNCHING, approved=[_approved("t1")], stranded=[_STRANDED], phase_branches=[_BRANCH]), _NOW),
    "closes": (
        _facts(
            todo_initiatives=[{"id": "d", "task_states": ["done", "dropped"]}, {"id": "e", "task_states": ["dropped"]}],
            approved=[{**_approved("m1", "mm"), "land_manual": True}],
            initiatives=[_started("o")],
            run_exited={"o": True},
            merged_prs=[{"url": "https://forge.example/pr/9", "repo": "r", "task_ids": ["o-t"]}],
        ),
        _NOW,
    ),
    "widen": (
        _facts(
            handoff_stops=[{"initiative": "i", "task_id": "t1", "state": "handoff", "reason": _ACCESSOR_STOP, "surfaces": ["src/ui/regatta.rs"], "widenings": 0}],
            initiatives=[_started("i")],
            run_exited={"i": True},
        ),
        _NOW,
    ),
    "a schedule update": (_schedule_facts(), _SCHEDULE_NOW),
    "a phase initiative waits for its worker": (
        _facts(initiatives=[{**_unstarted("p"), "lane_model": "phase"}, _unstarted("t")]),
        _NOW,
    ),
    "a stall alarm raised": (_facts(stall_alarm=_alarm_facts(last_land_at=_ts(7), open_initiatives={"alpha": _ts(30)})), _ALARM_NOW),
    "a stall alarm cleared": (_facts(stall_alarm=_alarm_facts(open=_OPEN)), _ALARM_NOW),
    "a repeat stall alarm at the hard stop": (_facts(limits={**_LIMITS, "hard_stop": True}, stall_alarm=_alarm_facts(streaks=[_STREAK])), _ALARM_NOW),
    "a stuck steer head": (
        _steer_facts(run_exited={"a": False}, initiatives=[_steer_initiative("a", True, ["f"]), _steer_initiative("b", False, ["f"])]),
        _STEER_NOW,
    ),
}


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
@pytest.mark.parametrize("clocked", [False, True], ids=["no clock", "clock"])
@pytest.mark.parametrize("held", [frozenset(), frozenset({"a", "i", "w", "x"})], ids=["unheld", "ci held"])
def test_the_split_plans_the_same_actions_in_the_same_order_as_before(name: str, clocked: bool, held: frozenset[str]):
    facts, clock = _SCENARIOS[name]
    now = clock if clocked else None
    assert _plan_as_holder(facts, now, held) == legacy_plan_as_holder(facts, now, held)


def _markers(facts: Facts, now: datetime) -> set[str]:
    """What one scenario reaches: each planned action's kind and cause, and each basis gate or launch slot it fills."""
    basis = tick_basis(facts, now)
    launch = _launch_on(basis)
    planned = _plan_as_holder(facts, now)
    gates = {"waiting": basis.waiting, "lease_held": basis.lease_held, "gated": basis.gated, "stuck_heads": launch.stuck_heads}
    return {
        *(str(a["kind"]) for a in planned),
        *(f"cause:{a['cause']}" for a in planned if "cause" in a),
        *(name for name, value in gates.items() if value),
    }


# The branch each scenario was added for. `deferred_land_deadlock` always returns None today, so no facts reach the
# deadlock line; the waiting scenario is built on the deadlock test's facts and pins the rest of that path.
_REACHES: dict[str, set[str]] = {
    "launch fills free lanes": {"launch_epic", "relaunch"},
    "quarantine": {"cause:scope"},
    "triage, a hold and a retry beside a decompose": {"request_triage", "hold_task", "retry", "launch_decompose"},
    "land and watch": {"land_phase", "revert_land"},
    "hard stop": {"land_phase", "revert_land", "unlaunched"},
    "housekeeping ages": {"stale_to_draft", "stalled_usr1"},
    "decompose": {"cause:empty_decompose"},
    "lost and unfetched runs": {"fetch_exit"},
    "waiting, idle stall and a held lease": {"waiting", "cause:idle_stall", "lease_held"},
    "a lease stuck past its ttl": {"cause:lease_stuck", "lease_held"},
    "gated initiatives": {"gated"},
    "steer cycle and approval restores": {"restore_approvals", "steer_clear"},
    "idle ready with no lane": {"cause:idle_ready"},
    "idle ready at the hard stop": {"cause:idle_ready"},
    "carries and rebases": {"carry_phase", "rebase_phase", "relaunch"},
    "closes": {"close_initiative", "finish_task", "close_merged_task"},
    "widen": {"widen_ticket"},
    "a schedule update": {"schedule_updated"},
    "a phase initiative waits for its worker": {"launch_epic", "unlaunched"},
    "a stall alarm raised": {"stall_alarm"},
    "a stall alarm cleared": {"stall_clear"},
    "a repeat stall alarm at the hard stop": {"stall_alarm"},
    "a stuck steer head": {"cause:steer_head_stuck", "steer_clear", "stuck_heads"},
}


@pytest.mark.parametrize("name", sorted(_REACHES))
def test_each_scenario_reaches_its_branch(name: str):
    facts, now = _SCENARIOS[name]
    assert _REACHES[name] <= _markers(facts, now)


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
def test_no_planner_writes_to_the_facts_it_plans_from(name: str):
    facts, now = _SCENARIOS[name]
    before = copy.deepcopy(facts)
    _plan_as_holder(facts, now, frozenset({"a"}))
    assert facts == before


def test_the_basis_and_the_launch_gates_hold_no_list_dict_or_set():
    facts, now = _SCENARIOS["waiting, idle stall and a held lease"]
    basis = tick_basis(facts, now)
    gates = launch_gates(plan_land_kind(basis), plan_decompose_kind(basis), plan_quarantine_kind(basis))
    for record in (basis, gates):
        fields = {k: v for k, v in vars(record).items() if k != "facts"}
        assert [k for k, v in fields.items() if isinstance(v, (list, dict, set))] == []
    with pytest.raises(dataclasses.FrozenInstanceError):
        basis.held = frozenset()  # type: ignore[misc]


def test_watch_reverts_a_landed_commit_whose_main_ci_is_red():
    reverts = plan_watch_kind(tick_basis(_facts(landed_main=[_RED_MAIN]), _NOW)).reverts
    assert [a["kind"] for a in reverts] == ["revert_land"]


def test_land_plans_a_land_phase_for_a_done_phase_and_nothing_else_of_the_tick():
    land = plan_land_kind(tick_basis(_facts(approved=[_approved("t1")], initiatives=[_unstarted("a")]), _NOW))
    assert [a["kind"] for a in land.lands] == ["land_phase"]
    assert land.carries == land.fetch_exits == land.deadlock_chair == []


def test_decompose_reports_an_empty_decompose_to_the_chair():
    plan = plan_decompose_kind(tick_basis(_facts(empty_decompose=[{"initiative": "m", "run": "m-1"}]), _NOW))
    assert [(a["kind"], a.get("cause")) for a in plan.empty_needs_chair] == [("needs_chair", "empty_decompose")]
    assert plan.stalled_intake == frozenset()


def test_quarantine_sends_a_scope_quarantine_to_the_chair_and_relaunches_nothing():
    facts = _facts(quarantines=[{"task_id": "p", "initiative": "m", "cause": "scope", "harness_failures": 0}])
    plan = plan_quarantine_kind(tick_basis(facts, _NOW))
    assert [(a["kind"], a.get("initiative")) for a in plan.recovered] == [("needs_chair", "m")]


def _launch_on(basis: TickBasis) -> LaunchPlan:
    return plan_launch_kind(basis, launch_gates(plan_land_kind(basis), plan_decompose_kind(basis), plan_quarantine_kind(basis)))


def _launch(facts: Facts) -> LaunchPlan:
    return _launch_on(tick_basis(facts, _NOW))


def test_launch_starts_an_epic_for_an_unstarted_initiative_with_ready_work():
    launch = _launch(_facts(initiatives=[_unstarted("a")]))
    assert [(a["kind"], a.get("initiative")) for a in launch.launches] == [("launch_epic", "a")]
    assert launch.launch_paused is False


def test_launch_at_the_hard_stop_launches_nothing_and_names_the_stop():
    launch = _launch(_facts(limits={**_LIMITS, "hard_stop": True}, initiatives=[_unstarted("a")]))
    assert launch.launches == []
    assert launch.launch_paused is True
    assert {a["reason"] for a in launch.stopped} == {"hard stop: nothing launches until the stop lifts"}


def test_housekeeping_moves_a_stale_ticket_to_draft_and_needs_the_clock():
    facts = _facts(stale_candidates=[_stale_row("a", "a-1")])
    basis = tick_basis(facts, _NOW)
    assert [a["kind"] for a in plan_housekeeping_kind(basis, _launch(facts)).stale] == ["stale_to_draft"]
    clockless = tick_basis(facts, None)
    assert plan_housekeeping_kind(clockless, _launch(facts)).stale == []
