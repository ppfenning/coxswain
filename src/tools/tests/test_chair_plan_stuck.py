from agent_tools.chair_plan_stuck import failed_recovery_phases, requeue_no_run, stuck_lands


def _streak(ticks: int, reason: str = "conflict") -> dict:
    return {"initiative": "x", "phase": "p1", "kind": "land_phase", "reason": reason, "ticks": ticks}


def _no_run(count: int) -> dict:
    task = {"id": "t1", "initiative": "x", "phase": "p1"}
    return {"approved": [task], "no_run_streaks": {"t1": count}}


def test_two_ticks_plan_nothing():
    assert stuck_lands({"land_fail_streaks": [_streak(2)]}) == ([], frozenset())


def test_three_ticks_plan_one_needs_chair_carrying_the_reason():
    actions, dropped = stuck_lands({"land_fail_streaks": [_streak(3)]})
    assert actions == [
        {
            "kind": "needs_chair",
            "initiative": "x",
            "phase": "p1",
            "cause": "stuck_land",
            "reason": "land_phase failed 3 ticks running: conflict",
        }
    ]
    assert dropped == {("x", "p1")}


def test_a_changed_reason_restarts_the_streak_and_plans_nothing():
    assert stuck_lands({"land_fail_streaks": [_streak(1, "other")]}) == ([], frozenset())


def test_a_no_run_count_of_three_plans_one_requeue_task():
    assert requeue_no_run(_no_run(3)) == [
        {
            "kind": "requeue_task",
            "initiative": "x",
            "phase": "p1",
            "task_id": "t1",
            "reason": "approved with no run to land from for 3 ticks; rebuilding",
        }
    ]


def test_a_no_run_count_of_two_plans_none():
    assert requeue_no_run(_no_run(2)) == []


def test_recovery_lands_are_held_and_launches_are_not():
    planned = [
        {"kind": "land_phase", "initiative": "a", "phase": "p1"},
        {"kind": "partial_land", "initiative": "b", "phase": "p2"},
        {"kind": "merge_level", "initiative": "c", "phase": "p3"},
        {"kind": "launch", "initiative": "d", "phase": "p4"},
    ]
    assert failed_recovery_phases(planned) == {("a", "p1"), ("b", "p2"), ("c", "p3")}
