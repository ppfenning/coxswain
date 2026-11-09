from agent_tools.chair_facts import approval_holds_from_actions, resolve_approval_hold_ticks, running_initiatives


def _item(initiative: str, task: str, state: str, surfaces: list[str]) -> dict:
    return {"initiative": initiative, "id": task, "state": state, "surfaces": surfaces, "merged": False}


def _task(task_id: str, initiative: str = "x", merged: bool = False) -> dict:
    return {
        "id": task_id,
        "initiative": initiative,
        "repo": "/r",
        "phase": "p1",
        "phase_done": False,
        "needs": [],
        "run": "run1",
        "needs_fetch": False,
        "merged": merged,
    }


def test_a_stale_approval_only_entry_is_not_live_and_a_live_initiative_and_phase_are():
    items = [
        _item("x", "a", "approved", ["a.rs"]),
        _item("y", "b", "ready", ["b.rs"]),
        _item("z", "c", "ready", ["c.rs"]),
    ]
    running = running_initiatives({"y"}, items, {"x": "/r"}, unlanded={"x"}, live_phases=[("z", "p1")])
    assert [(e["id"], e.get("phase"), e["live"]) for e in running] == [
        ("x", None, False),
        ("y", None, True),
        ("z", "p1", True),
    ]


def test_hold_age_counts_land_phase_and_steer_rows_since_the_last_landed_row():
    rows = [
        {"ts": "2026-11-09T01:00:00Z", "kind": "land_phase", "initiative": "x", "status": "refused"},
        {"ts": "2026-11-09T01:30:00Z", "kind": "status", "status": "recorded"},
        {"ts": "2026-11-09T02:00:00Z", "kind": "land_phase", "initiative": "x", "status": "landed"},
        {"ts": "2026-11-09T02:30:00Z", "kind": "status", "status": "recorded"},
        {"ts": "2026-11-09T03:00:00Z", "kind": "land_phase", "initiative": "x", "status": "refused"},
        {"ts": "2026-11-09T03:30:00Z", "kind": "status", "status": "recorded"},
        {"ts": "2026-11-09T04:00:00Z", "kind": "steer_clear", "initiative": "x", "other": "y"},
        {"ts": "2026-11-09T04:30:00Z", "kind": "status", "status": "recorded"},
        {"ts": "2026-11-09T05:00:00Z", "kind": "land", "task_id": "a", "initiative": "x", "status": "not_landed"},
        {"ts": "2026-11-09T05:15:00Z", "kind": "pull"},
        {"ts": "2026-11-09T05:30:00Z", "kind": "status", "status": "recorded"},
        {"ts": "2026-11-09T06:00:00Z", "kind": "land_phase", "initiative": "other", "status": "refused"},
    ]
    assert approval_holds_from_actions([_task("a")], rows) == [
        {"initiative": "x", "repo": "/r", "task_id": "a", "run": "run1", "ticks": 3}
    ]


def test_rows_of_one_tick_with_different_ts_age_the_approval_once_and_a_sibling_land_does_not_reset_it():
    rows = [
        {"ts": "2026-11-09T01:00:00Z", "kind": "steer_clear", "initiative": "x", "other": "y"},
        {"ts": "2026-11-09T01:00:07Z", "kind": "steer_clear", "initiative": "x", "other": "z"},
        {"ts": "2026-11-09T01:03:41Z", "kind": "land_phase", "initiative": "x", "status": "refused"},
        {"ts": "2026-11-09T01:04:00Z", "kind": "status", "status": "recorded"},
        {"ts": "2026-11-09T02:00:00Z", "kind": "land", "task_id": "b", "initiative": "x", "status": "landed"},
        {"ts": "2026-11-09T02:00:30Z", "kind": "status", "status": "recorded"},
        {"ts": "2026-11-09T03:00:00Z", "kind": "land", "task_id": "b", "initiative": "x", "status": "refused"},
        {"ts": "2026-11-09T03:00:30Z", "kind": "status", "status": "recorded"},
    ]
    assert [(h["task_id"], h["ticks"]) for h in approval_holds_from_actions([_task("a"), _task("b")], rows)] == [
        ("a", 1),
        ("b", 1),
    ]


def test_hold_age_skips_a_merged_task_and_a_task_with_no_rows():
    rows = [{"ts": "2026-11-09T01:00:00Z", "kind": "land", "task_id": "a", "initiative": "x", "status": "refused"}]
    assert approval_holds_from_actions([_task("a", merged=True), _task("b", "w")], rows) == []


def test_hold_age_reads_an_action_json_store_row():
    rows = [{"ts": "2026-11-09T01:00:00Z", "action_json": '{"kind": "steer_clear", "initiative": "x", "other": "y"}'}]
    assert [h["ticks"] for h in approval_holds_from_actions([_task("a")], rows)] == [1]


def test_the_hold_limit_defaults_to_6_unless_given_a_positive_number():
    values = (None, "x", 0, -3, True, float("inf"), 4, "5")
    assert [resolve_approval_hold_ticks(v) for v in values] == [6, 6, 6, 6, 6, 6, 4, 5]
