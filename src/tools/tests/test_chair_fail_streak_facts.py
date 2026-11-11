from agent_tools.chair_facts import land_fail_streaks_from_actions, no_run_streaks_from_actions

TASK = {"id": "t1", "initiative": "x"}


def _land(ts: str, status: str, reason: str = "", kind: str = "land_phase") -> dict:
    return {"ts": ts, "kind": kind, "status": status, "initiative": "x", "phase": "p1", "reason": reason}


def _no_run(ts: str) -> dict:
    return {"ts": ts, "kind": "needs_chair", "cause": "no_run", "initiative": "x"}


def test_three_identical_reasons_give_ticks_three():
    rows = [_land(f"2026-11-10T10:0{n}:00Z", "refused", "conflict") for n in range(3)]
    assert land_fail_streaks_from_actions(rows) == [
        {"initiative": "x", "phase": "p1", "kind": "land_phase", "reason": "conflict", "ticks": 3}
    ]


def test_a_changed_reason_restarts_at_one():
    rows = [
        _land("2026-11-10T10:00:00Z", "failed", "a"),
        _land("2026-11-10T10:01:00Z", "failed", "a"),
        _land("2026-11-10T10:02:00Z", "failed", "b"),
    ]
    assert land_fail_streaks_from_actions(rows)[0]["ticks"] == 1


def test_a_landed_row_clears_and_merge_level_keeps_its_kind():
    rows = [_land("2026-11-10T10:00:00Z", "failed", "a"), _land("2026-11-10T10:01:00Z", "landed")]
    assert land_fail_streaks_from_actions(rows) == []
    merge = [_land("2026-11-10T10:00:00Z", "merge_failed", "a", kind="merge_level")]
    assert land_fail_streaks_from_actions(merge)[0]["kind"] == "merge_level"


def test_a_no_run_streak_counts_three_and_a_landed_row_clears_it():
    rows = [_no_run(f"2026-11-10T10:0{n}:00Z") for n in range(3)]
    assert no_run_streaks_from_actions([TASK], rows) == {"t1": 3}
    landed = [*rows, _land("2026-11-10T10:05:00Z", "landed")]
    assert no_run_streaks_from_actions([TASK], landed) == {}
