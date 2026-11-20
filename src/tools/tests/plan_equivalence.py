"""Run the chair's holder plan as it stood before C1 beside today's, on every call a suite makes, and fail on a difference.

Opt in per run; nothing loads it by default:

    cd src/tools && pytest -n 4 -p tests.plan_equivalence tests/test_chair_*.py

With the plugin loaded, each call to `chair_plan._plan_as_holder` also runs `legacy_plan_as_holder` on the same facts
and raises AssertionError in the calling test when the two lists differ. The legacy copy runs in `chair_plan`'s own
namespace, so a test that monkeypatches a planner there patches both. The terminal summary names how many calls it
compared, summed over xdist workers. `test_chair_plan_kinds` imports the legacy copy for its fixed scenarios.

Delete this module when the queue workers (C3-C5) replace the tick.
"""

import types
from datetime import datetime
from typing import Any

import pytest

from agent_tools import chair_approval_restore, chair_plan, chair_steer_cycle
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
    _planned_schedule,
    _schedule_update,
    _steer_deferred,
    _stuck_steer_heads,
    _unlaunched_actions,
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
    plan_deferred_land_deadlock,
    plan_fill,
    plan_finish,
    plan_held_files,
    plan_idle_ready,
    plan_idle_stall,
    plan_lands,
    plan_lost_runs,
    plan_missed_closes,
    plan_no_progress,
    plan_rebase,
    plan_recover,
    plan_review,
    plan_stale,
    plan_stall,
    plan_stall_alarm,
    plan_tune,
    plan_widen,
    refused_launches,
    revert_check,
    steer_heads,
    waiting_initiatives,
)
from agent_tools.chair_types import Action, Facts
from agent_tools.queue_rows import launchable_by_tick

_KEY = "plan_equivalence_calls"


def legacy_plan_as_holder(raw_facts: Facts, now: datetime | None, held: frozenset[str] = frozenset()) -> list[Action]:
    # `_unlocked` as it stood, inlined: the split removed it.
    unlocked = _with_dead_pid_lost(raw_facts, now)
    facts, releases = chair_steer_cycle.break_cycle(unlocked)[0], list(chair_approval_restore.plan_restore(unlocked))
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
        # F2 (#1277), carried into the legacy copy so it compares the split, not F2's rule.
        "waits for a phase worker": frozenset(i["id"] for i in facts["initiatives"] if not launchable_by_tick(i)),
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


def _compared(config: pytest.Config) -> int:
    return int(getattr(config, "_plan_equivalence_calls", 0))


def pytest_configure(config: pytest.Config) -> None:
    current = chair_plan._plan_as_holder
    legacy = types.FunctionType(legacy_plan_as_holder.__code__, vars(chair_plan), "legacy_plan_as_holder", legacy_plan_as_holder.__defaults__)

    def both(raw_facts: Facts, now: datetime | None, held: frozenset[str] = frozenset()) -> list[Action]:
        planned = current(raw_facts, now, held)
        before = legacy(raw_facts, now, held)
        config._plan_equivalence_calls = _compared(config) + 1  # type: ignore[attr-defined]
        assert planned == before, f"the split holder plan differs from the pre-C1 plan:\n now:    {planned}\n before: {before}"
        return planned

    chair_plan._plan_as_holder = both  # type: ignore[assignment]


def pytest_sessionfinish(session: pytest.Session) -> None:
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None:
        workeroutput[_KEY] = _compared(session.config)


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: object) -> None:
    """xdist's controller: add each finished worker's count to its own."""
    node.config._plan_equivalence_calls = _compared(node.config) + int(getattr(node, "workeroutput", {}).get(_KEY, 0))


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter, config: pytest.Config) -> None:
    terminalreporter.write_line(f"plan_equivalence: {_compared(config)} holder plans compared with the pre-C1 plan, all equal")
