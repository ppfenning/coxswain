"""C1: the holder's tick is planned per kind of work, and plans the same actions, in the same order, as before the split.

`_legacy_plan_as_holder` is `chair_plan._plan_as_holder` as it stood before the split, verbatim but for its name. Each
scenario runs both on the same facts, with and without the tick's clock and a CI-gate hold. Delete the legacy copy when
the queue workers (C3-C5) replace the tick; the per-kind tests below stay.
"""

from datetime import UTC, datetime

import pytest

from agent_tools.chair_plan import (
    _LAUNCHES,
    STRANDED_CAUSE,
    _cap_count,
    _cap_launches,
    _decompose_gate,
    _decompose_miss_gate,
    _dispatch_room,
    _empty_decompose_needs_chair_actions,
    _fetch_exit_actions,
    _free_lanes,
    _gated_initiatives,
    _hold_for_ci,
    _is_launch,
    _landing_initiatives,
    _launch_cap,
    _lease_held_ids,
    _lease_stuck_actions,
    _login_needs_chair_actions,
    _needs_chair_only,
    _no_host,
    _plan_as_holder,
    _planned_schedule,
    _schedule_update,
    _steer_deferred,
    _stuck_steer_heads,
    _unlaunched_actions,
    _unlocked,
    _unmet_needs_ids,
    _unstarted_waiting_chair,
    _waiting_actions,
    _with_carry,
    _with_dead_pid_lost,
    _with_rebases,
    _withhold_lease_held,
    _withhold_lost_runs,
    _withhold_not_exited,
    _withhold_remote_unfetched,
    _within_cap,
    _without_carried_lands,
    _without_deadlocked,
    _without_gated_lands,
    _without_gated_launches,
    _without_widened,
    action_text,
    backoff_held,
    budget_skips,
    claimed_by,
    deferred_land_deadlock,
    host_free_slots,
    plan_carry,
    plan_close_initiatives,
    plan_decompose_kind,
    plan_deferred_land_deadlock,
    plan_fill,
    plan_finish,
    plan_held_files,
    plan_housekeeping_kind,
    plan_idle_ready,
    plan_idle_stall,
    plan_land_kind,
    plan_lands,
    plan_launch_kind,
    plan_lost_runs,
    plan_missed_closes,
    plan_no_progress,
    plan_quarantine_kind,
    plan_rebase,
    plan_recover,
    plan_review,
    plan_stale,
    plan_stall,
    plan_stall_alarm,
    plan_tune,
    plan_watch_kind,
    plan_widen,
    refused_launches,
    revert_check,
    steer_heads,
    tick_basis,
    waiting_initiatives,
)
from agent_tools.chair_types import Action, Facts

_NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def _legacy_plan_as_holder(raw_facts: Facts, now: datetime | None, held: frozenset[str] = frozenset()) -> list[Action]:
    facts, releases = _unlocked(_with_dead_pid_lost(raw_facts, now))
    schedule = _planned_schedule(facts, now)
    schedule_update = _schedule_update(facts, schedule, now)
    reverts = revert_check(facts.get("landed_main", []))
    carries = plan_carry(facts.get("stranded", []))
    gated = _gated_initiatives(facts)
    planned_lands = _without_carried_lands(_without_gated_lands(plan_lands(facts, now), gated), carries)
    fetch_exits = _fetch_exit_actions(facts)
    login_needs_chair = _login_needs_chair_actions(facts, now)
    empty_decompose_needs_chair = _empty_decompose_needs_chair_actions(facts)
    held_needs_chair = plan_held_files(facts)
    no_progress_holds = plan_no_progress(facts)
    no_progress = frozenset(action_text(a, "initiative") for a in no_progress_holds)
    streak_stalled, streak_needs_chair = _decompose_gate(facts)
    missed_intake, miss_needs_chair = _decompose_miss_gate(facts, now)
    stalled_intake = streak_stalled | missed_intake
    decompose_stalled_needs_chair = [*streak_needs_chair, *miss_needs_chair]
    review = plan_review(facts)
    planned_unstarted_waiting_chair = _unstarted_waiting_chair(facts["initiatives"])
    lost = frozenset(facts.get("lost_runs", {}))
    stale = plan_stale(facts, now) if now is not None else []
    tune = plan_tune(facts, now) if now is not None else []
    stall = plan_stall(facts.get("stall_candidates", []), now) if now is not None else []
    planned_idle_stall = plan_idle_stall(facts.get("idle_stall"), now) if now is not None else []
    deadlock = deferred_land_deadlock(facts, now)
    deadlock_chair = plan_deferred_land_deadlock(facts, now)
    remote_unfetched = frozenset(facts.get("remote_unfetched", {}))
    run_exited = facts.get("run_exited", {})
    widen = plan_widen(facts)
    widened = frozenset((action_text(a, "initiative"), action_text(a, "task_id")) for a in widen)
    lands = planned_lands
    recover_actions = plan_recover(facts, now)
    lost_actions = plan_lost_runs(facts, now)
    ordinary = _withhold_lost_runs(_without_widened(recover_actions, widened), lost)
    pre_exit_gate = _withhold_remote_unfetched(ordinary, remote_unfetched)
    would_relaunch = frozenset(action_text(a, "initiative") for a in pre_exit_gate if a["kind"] == "relaunch")
    not_exited = frozenset(i for i in would_relaunch if not run_exited.get(i, False))
    recoverable = _without_gated_launches(
        _hold_for_ci([*_withhold_not_exited(pre_exit_gate, not_exited), *lost_actions], held | gated | no_progress), gated | no_progress
    )
    lease_held = _lease_held_ids(facts)
    lease_stuck = _lease_stuck_actions(facts, now)
    waiting = waiting_initiatives(facts)
    waiting_ids = frozenset(w.initiative for w in waiting)
    idle_stall = [] if deadlock is not None else planned_idle_stall
    waiting_actions = _without_deadlocked(_waiting_actions(waiting, facts.get("waiting_reported", {})), deadlock)
    unstarted_waiting_chair = _without_deadlocked(planned_unstarted_waiting_chair, deadlock)
    closes = [*plan_close_initiatives(facts), *plan_finish(facts), *plan_missed_closes(facts)]
    recovered = _withhold_lease_held(
        [a for a in _without_deadlocked(recoverable, deadlock) if not (a["kind"] in ("relaunch", "clear_branches") and action_text(a, "initiative") in waiting_ids)],
        lease_held,
    )
    if facts["limits"]["hard_stop"]:
        stopped: list[Action] = [
            {**a, "reason": "hard stop: nothing launches until the stop lifts"}
            for a in _unlaunched_actions(facts, [], {}, {}, 0, False, 0, now, [])
        ]
        idle_ready = plan_idle_ready(facts, recovered, now) if now is not None else []
        stopped_plan = [
            *reverts,
            *lands,
            *fetch_exits,
            *stale,
            *tune,
            *stall,
            *idle_stall,
            *idle_ready,
            *releases,
            *deadlock_chair,
            *carries,
            *_needs_chair_only(recovered),
            *widen,
            *login_needs_chair,
            *empty_decompose_needs_chair,
            *lease_stuck,
            *held_needs_chair,
            *no_progress_holds,
            *decompose_stalled_needs_chair,
            *review,
            *unstarted_waiting_chair,
            *waiting_actions,
            *closes,
            *stopped,
            *schedule_update,
        ]
        return [*stopped_plan, *(plan_stall_alarm(facts, stopped_plan, now, launch_paused=True) if now is not None else [])]
    cap = _launch_cap(facts["limits"])
    host_free = host_free_slots(facts)
    capped_actions, consumed = _cap_launches(
        recovered, min(cap, _dispatch_room(facts["dispatch"])), facts["initiatives"], host_free, facts.get("home", {}), facts
    )
    capped = [_with_carry(a, facts["approved"]) for a in _within_cap(capped_actions, cap)]
    kept = sum(a["kind"] in _LAUNCHES and "host" not in a for a in capped)
    decompose_only = frozenset(facts.get("remote_unfetched_decompose", ()))
    withheld_by = {
        "recovery already holds it": frozenset(
            {action_text(a, "initiative") for a in capped if a["kind"] == "relaunch" or a["kind"] == "steer_clear" or _steer_deferred(a)}
        ),
        "quarantined": frozenset(q["initiative"] for q in facts["quarantines"] if q["cause"] != STRANDED_CAUSE),
        "stranded": frozenset(q["initiative"] for q in facts["quarantines"] if q["cause"] == STRANDED_CAUSE),
        "its branches are not fetched": remote_unfetched - decompose_only,
        "its last run has not exited": not_exited,
        "its run is lost": lost,
        "schema version death": frozenset(facts.get("schema_deaths", {})),
        "an unmet need": _unmet_needs_ids(facts["initiatives"]),
        "its repo is landing": _landing_initiatives(facts, planned_lands),
        "relaunch refused by the budget or the loop rule": refused_launches(facts, now),
        "land backoff": backoff_held(facts, now),
        "waiting on a need": waiting_ids,
        "its launch was refused on a held lease": lease_held,
        "it is held or deferred to a later release": gated,
        "its last runs ended the same way": no_progress,
    }
    withheld = frozenset[str]().union(*withheld_by.values())
    claimed = claimed_by(recover_actions, facts)
    free_lanes = _free_lanes(cap, kept, facts["dispatch"], _cap_count(capped) - kept)
    fill_schedule = _planned_schedule(facts, now, withheld) if withheld else schedule
    planned_fill = plan_fill(facts, free_lanes, withheld, consumed, claimed, stalled=stalled_intake, now=now, schedule=fill_schedule)
    filled = _within_cap(planned_fill, cap - _cap_count(capped))
    launches = [*capped, *filled]
    skips = budget_skips(facts, facts["initiatives"], now)
    no_host = _no_host(free_lanes, host_free, consumed, filled, facts["dispatch"].get("local_lanes") == "decompose")
    stuck_heads = _stuck_steer_heads(
        steer_heads(launches),
        frozenset(action_text(a, "initiative") for a in launches if _is_launch(a)),
        frozenset(r["id"] for r in facts.get("running", [])),
        {i["id"]: i for i in facts["initiatives"]},
        withheld_by,
        skips,
        no_host,
    )
    cap_left = max(0, cap - _cap_count(launches))
    unlaunched = _unlaunched_actions(
        facts, launches, withheld_by, skips, free_lanes, no_host, cap_left, now, stuck_heads, schedule
    )
    relaunching = frozenset(action_text(a, "initiative") for a in [*recovered, *launches] if _is_launch(a))
    rebases = plan_rebase(facts.get("phase_branches", []), relaunching)
    idle_ready = plan_idle_ready(facts, [*launches, *stuck_heads, *unlaunched], now) if now is not None else []
    planned = [
        *reverts,
        *lands,
        *fetch_exits,
        *stale,
        *tune,
        *stall,
        *idle_stall,
        *idle_ready,
        *releases,
        *deadlock_chair,
        *carries,
        *_with_rebases(launches, rebases),
        *widen,
        *login_needs_chair,
        *empty_decompose_needs_chair,
        *lease_stuck,
        *held_needs_chair,
        *no_progress_holds,
        *decompose_stalled_needs_chair,
        *review,
        *unstarted_waiting_chair,
        *waiting_actions,
        *stuck_heads,
        *unlaunched,
        *closes,
        *schedule_update,
    ]
    return [*planned, *(plan_stall_alarm(facts, planned, now) if now is not None else [])]


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

# Each scenario leans on one or more kinds; together they reach every kind's planner and both branches of the limits gate.
_SCENARIOS: dict[str, Facts] = {
    "empty": _facts(),
    "launch fills free lanes": _facts(initiatives=[_unstarted("a"), _unstarted("b"), _started("c")], run_exited={"c": True}),
    "launch cap binds": _facts(
        limits={**_LIMITS, "launch_cap": 1}, initiatives=[_started("a"), _started("b"), _unstarted("c")], run_exited={"a": True, "b": True}
    ),
    "quarantine and waiting": _facts(
        initiatives=[_unstarted("q"), _started("w", ("z",)), _unstarted("u")],
        quarantines=[
            {"task_id": "q-t", "initiative": "q", "cause": "scope", "harness_failures": 0},
            {"task_id": "k-t", "initiative": "k", "cause": "harness", "harness_failures": 1, "has_patch": False, "rescue_failed": False},
        ],
    ),
    "land and watch": _facts(approved=[_approved("t1"), _approved("t2", "y")], landed_main=[_RED_MAIN], initiatives=[_started("x")]),
    "hard stop": _facts(
        limits={**_LIMITS, "hard_stop": True, "weekly_fraction": 0.95},
        approved=[_approved("t1")],
        initiatives=[_started("i"), _unstarted("j")],
        quarantines=[{"task_id": "p", "initiative": "m", "cause": "scope", "harness_failures": 0}],
        intake=["n1"],
        landed_main=[_RED_MAIN],
    ),
    "housekeeping ages": _facts(
        stale_candidates=[_stale_row("a", "a-1"), _stale_row("b", "b-1")],
        stall_candidates=[_STALL],
        initiatives=[_started("a")],
        run_exited={"a": True},
    ),
    "decompose": _facts(empty_decompose=[{"initiative": "m", "run": "m-1"}], intake=["n1", "n2"]),
    "lost and unfetched runs": _facts(
        initiatives=[_started("l"), _started("f"), _started("e")],
        lost_runs={"l": "l-2"},
        remote_unfetched={"f": "f-3"},
        run_hosts={"f-3": "lane-1"},
        run_exited={"e": False},
    ),
}


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
@pytest.mark.parametrize("now", [None, _NOW], ids=["no clock", "clock"])
@pytest.mark.parametrize("held", [frozenset(), frozenset({"i", "w", "x"})], ids=["unheld", "ci held"])
def test_the_split_plans_the_same_actions_in_the_same_order_as_before(name: str, now: datetime | None, held: frozenset[str]):
    facts = _SCENARIOS[name]
    assert _plan_as_holder(facts, now, held) == _legacy_plan_as_holder(facts, now, held)


def test_the_scenarios_reach_every_kind():
    planned = {a["kind"] for facts in _SCENARIOS.values() for a in _plan_as_holder(facts, _NOW)}
    assert {"revert_land", "land_phase", "stale_to_draft", "stalled_usr1", "needs_chair", "relaunch", "launch_epic", "fetch_exit"} <= planned


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


def _launch(facts: Facts):
    basis = tick_basis(facts, _NOW)
    return plan_launch_kind(basis, plan_land_kind(basis), plan_decompose_kind(basis), plan_quarantine_kind(basis))


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
