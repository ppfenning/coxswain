from agent_tools.chair_plan_stuck import failed_recovery_phases, requeue_no_run, stuck_lands

TASK = {"id": "t1", "initiative": "x", "phase": "p1"}


def _streak(ticks: int, reason: str = "conflict") -> dict:
    return {"initiative": "x", "phase": "p1", "kind": "land_phase", "reason": reason, "ticks": ticks}


def test_two_ticks_plans_nothing():
    assert stuck_lands({"land_fail_streaks": [_streak(2)]}) == ([], frozenset())


def test_three_ticks_plans_one_needs_chair_carrying_the_reason():
    actions, dropped = stuck_lands({"land_fail_streaks": [_streak(3)]})
    assert [(a["kind"], a["initiative"], a["phase"], a["cause"]) for a in actions] == [("needs_chair", "x", "p1", "stuck_land")]
    assert "land_phase" in actions[0]["reason"] and "conflict" in actions[0]["reason"]
    assert dropped == {("x", "p1")}


def test_same_entry_with_a_new_reason_plans_nothing():
    assert stuck_lands({"land_fail_streaks": [_streak(1, "red checks")]}) == ([], frozenset())


def test_no_run_count_of_three_plans_one_requeue_task():
    facts = {"approved": [TASK], "no_run_streaks": {"t1": 3}}
    assert requeue_no_run(facts) == [
        {
            "kind": "requeue_task", "initiative": "x", "phase": "p1", "task_id": "t1",
            "reason": "approved with no run to land from for 3 ticks; rebuilding",
        }
    ]


def test_no_run_count_of_two_plans_none():
    assert requeue_no_run({"approved": [TASK], "no_run_streaks": {"t1": 2}}) == []


def test_failed_recovery_phases_holds_the_three_kinds_only():
    planned = [
        {"kind": "land_phase", "initiative": "a", "phase": "p1"},
        {"kind": "partial_land", "initiative": "b", "phase": "p2"},
        {"kind": "merge_level", "initiative": "c", "phase": "p3"},
        {"kind": "launch", "initiative": "d", "phase": "p4"},
    ]
    assert failed_recovery_phases(planned) == {("a", "p1"), ("b", "p2"), ("c", "p3")}
