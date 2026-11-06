from datetime import UTC, datetime

from agent_tools.chair_plan import _plan_as_holder, _withhold_blocked_hosts, plan_tick_held
from agent_tools.chair_plan_routine import routine_only
from agent_tools.chair_types import stamp
from tests.test_chair_plan import _approved, _facts, _initiative, _kinds, _lease, _unstarted

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
LENT = _lease(lent=True)
STANDBY = {"kind": "standby", "holder": "other", "host": "elsewhere", "epoch": 7}


def _plan(**overrides) -> list[dict]:
    return plan_tick_held(_facts(lease=LENT, **overrides), NOW).actions


def test_an_exited_epic_with_a_ready_task_plans_one_relaunch_with_no_base_or_target():
    actions = _plan(initiatives=[_initiative("a")], run_exited={"a": True})
    relaunches = [a for a in actions if a["kind"] == "relaunch"]
    assert relaunches == [{"kind": "relaunch", "initiative": "a", "epoch": 7}]
    assert actions[-1] == STANDBY
    assert all(a["epoch"] == 7 for a in actions)


def test_a_live_run_plans_no_relaunch():
    actions = _plan(initiatives=[_initiative("a")], run_exited={"a": False})
    assert _kinds(actions) == ["standby"]


def test_all_tasks_approved_plans_a_land_phase_and_a_backoff_entry_withholds_it():
    approved = [_approved("t1")]
    assert _kinds(_plan(approved=approved)) == ["land_phase", "standby"]
    refusal = {"initiative": "x", "phase": "p", "run": "x-1", "attempts": 1, "last_refused_at": NOW.isoformat()}
    assert _kinds(_plan(approved=approved, land_refusals=[refusal])) == ["standby"]


def test_a_quarantined_task_plans_a_needs_chair_and_no_relaunch():
    quarantine = {"task_id": "a-t", "initiative": "a", "cause": "scope", "harness_failures": 0}
    actions = _plan(initiatives=[_initiative("a")], run_exited={"a": True}, quarantines=[quarantine])
    assert _kinds(actions) == ["needs_chair", "standby"]
    assert actions[0]["task_id"] == "a-t"


def test_a_ready_backlog_with_no_epic_to_relaunch_plans_no_launch_epic():
    actions = _plan(initiatives=[_unstarted("b")])
    assert _kinds(actions) == ["standby"]


def test_a_foreign_lease_that_is_not_lent_plans_only_standby():
    facts = {"initiatives": [_initiative("a")], "run_exited": {"a": True}, "approved": [_approved("t1")]}
    assert plan_tick_held(_facts(lease=_lease(), **facts), NOW).actions == [STANDBY]
    assert plan_tick_held(_facts(lease=_lease(lent=False), **facts), NOW).actions == [STANDBY]


def test_a_lease_that_is_mine_plans_exactly_what_the_holder_plans():
    facts = _facts(initiatives=[_initiative("a")], run_exited={"a": True}, approved=[_approved("t1")])
    facts = {**facts, "lease": {**facts["lease"], "lent": True}}
    expected = [stamp(a, 7) for a in _withhold_blocked_hosts(_plan_as_holder(facts, NOW), facts)]
    assert plan_tick_held(facts, NOW).actions[: len(expected)] == expected
    assert "standby" not in _kinds(plan_tick_held(facts, NOW).actions)


def test_routine_only_keeps_the_allowlist_in_order_and_drops_the_rest():
    allowed = ["relaunch", "clear_branches", "rebase_phase", "fetch", "fetch_exit", "land_phase", "needs_chair", "waiting"]
    dropped = ["launch_epic", "launch_decompose", "retry", "rescue", "partial_land", "land", "stale", "tune", "housekeeping", "check_login"]
    mixed = [{"kind": k} for k in [*dropped[:5], *allowed, *dropped[5:]]]
    assert _kinds(routine_only(mixed)) == allowed
