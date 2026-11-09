from agent_tools.chair_facts import (
    approval_holds_from_actions,
    resolve_approval_hold_ticks,
    running_initiatives,
)


def test_a_stale_approval_only_entry_is_not_live_and_a_live_entry_and_a_live_phase_are():
    items = [
        {"initiative": "a", "id": "a1", "state": "ready", "surfaces": ["a.rs"]},
        {"initiative": "b", "id": "b1", "state": "approved", "surfaces": ["b.rs"]},
        {"initiative": "c", "id": "c1", "state": "in_progress", "phase": "p1", "surfaces": ["c.rs"]},
    ]
    running = running_initiatives({"a"}, items, {}, unlanded={"b"}, live_phases=[("c", "p1")])
    assert [(r["id"], r.get("phase"), r["live"]) for r in running] == [("a", None, True), ("b", None, False), ("c", "p1", True)]


def test_hold_age_counts_distinct_ticks_since_the_last_landed_row_and_skips_merged_tasks():
    approved = [
        {"id": "t1", "initiative": "i", "repo": "r", "run": "i-1", "merged": False},
        {"id": "t2", "initiative": "i", "repo": "r", "run": "i-1", "merged": True},
    ]
    actions = [
        {"ts": "2026-11-09T01:00:00Z", "kind": "land", "task_id": "t1", "initiative": "i", "status": "refused"},
        {"ts": "2026-11-09T02:00:00Z", "kind": "land_phase", "task_id": "t1", "initiative": "i", "status": "landed"},
        {"ts": "2026-11-09T03:00:00Z", "kind": "land", "task_id": "t1", "initiative": "i", "status": "refused"},
        {"ts": "2026-11-09T04:00:00Z", "kind": "steer_clear", "initiative": "i", "other": "j"},
        {"ts": "2026-11-09T04:00:00Z", "kind": "steer_clear", "initiative": "i", "other": "k"},
        {"ts": "2026-11-09T05:00:00Z", "kind": "pull"},
        {"ts": "2026-11-09T06:00:00Z", "kind": "land", "task_id": "t9", "initiative": "i", "status": "refused"},
    ]
    assert approval_holds_from_actions(approved, actions) == [
        {"initiative": "i", "repo": "r", "task_id": "t1", "run": "i-1", "ticks": 2}
    ]


def test_the_approval_hold_limit_defaults_to_6_when_missing_non_numeric_or_not_positive():
    assert [resolve_approval_hold_ticks(v) for v in (None, "x", 0, -3, "4")] == [6, 6, 6, 6, 4]
