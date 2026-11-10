from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_chair_facts import _deps

from agent_tools import chair_read_docket, pacing
from agent_tools.chair import lease_holder
from agent_tools.chair_facts import (
    FactsDeps,
    gather_facts,
    read_initiative_repos,
    read_ticket_items,
    resolve_serial_files,
    resolve_shared_file_limit,
    running_initiatives,
    steer_streaks_from_actions,
    ticket_surfaces,
)
from agent_tools.chair_steer import steer_check

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
POLICY = pacing.Policy(
    pace_thresholds=(1.2, 1.5, 2.0),
    tier_ladder=("deep", "standard", "cheap"),
    effort_ladder=("high", "low"),
    min_headroom_usd=1.0,
    weekly_hard_stop_fraction=0.85,
)


def _item(initiative: str, task: str, state: str, surfaces: list[str], merged: bool = False) -> dict:
    return {"initiative": initiative, "id": task, "state": state, "surfaces": surfaces, "merged": merged}


def test_ready_surfaces_are_the_sorted_union_of_the_ready_tasks():
    items = [_item("x", "a", "ready", ["a.rs"]), _item("x", "b", "ready", ["b.rs", "a.rs"]), _item("x", "c", "ready", [])]
    assert ticket_surfaces(items, {"x": {"a", "b", "c"}}) == {"x": ["a.rs", "b.rs"]}


def test_a_task_outside_the_ready_set_adds_no_surface():
    items = [_item("x", "a", "ready", ["a.rs"]), _item("x", "d", "done", ["d.rs"])]
    assert ticket_surfaces(items, {"x": {"a"}}) == {"x": ["a.rs"]}


def test_an_approved_tasks_surfaces_count_for_a_running_initiative_and_a_done_tasks_do_not():
    items = [_item("x", "a", "approved", ["a.rs"]), _item("x", "d", "done", ["d.rs"]), _item("y", "b", "ready", ["b.rs"])]
    assert running_initiatives({"x"}, items, {"x": "/r"}) == [{"id": "x", "repo": "/r", "surfaces": ["a.rs"], "live": True}]


def test_a_non_live_unlanded_initiative_holds_only_its_approved_surfaces():
    items = [_item("x", "a", "approved", ["a.rs"]), _item("x", "b", "ready", ["b.rs"])]
    assert running_initiatives(set(), items, {"x": "/r"}, unlanded={"x"}) == [
        {"id": "x", "repo": "/r", "surfaces": ["a.rs"], "live": False}
    ]


def test_a_non_live_unlanded_initiative_with_only_ready_tasks_gives_no_entry():
    items = [_item("x", "b", "ready", ["b.rs"])]
    assert running_initiatives(set(), items, {"x": "/r"}, unlanded={"x"}) == []


def test_a_non_live_initiative_with_only_done_or_merged_tasks_gives_no_entry_and_no_steer():
    items = [_item("x", "a", "done", ["a.rs"]), _item("x", "b", "approved", ["a.rs"], merged=True)]
    running = running_initiatives(set(), items, {"x": "/r"}, unlanded={"x"})
    assert running == []
    assert steer_check("y", "/r", ["a.rs"], running, {}) is None


def test_an_approved_task_that_is_not_merged_still_holds_its_surfaces():
    items = [_item("x", "a", "approved", ["a.rs"], merged=False)]
    expected = {"id": "x", "repo": "/r", "surfaces": ["a.rs"]}
    assert running_initiatives({"x"}, items, {"x": "/r"}) == [{**expected, "live": True}]
    assert running_initiatives(set(), items, {"x": "/r"}, unlanded={"x"}) == [{**expected, "live": False}]


def test_a_live_initiative_holds_the_ready_task_and_not_the_merged_approved_one():
    items = [_item("x", "a", "approved", ["a.rs"], merged=True), _item("x", "b", "ready", ["b.rs"])]
    assert running_initiatives({"x"}, items, {"x": "/r"}) == [{"id": "x", "repo": "/r", "surfaces": ["b.rs"], "live": True}]


def test_an_idle_initiative_with_ready_tasks_on_a_file_does_not_defer_another_that_wants_it():
    items = [_item("a", "t1", "ready", ["f"]), _item("a", "t2", "ready", ["f"])]
    running = running_initiatives(set(), items, {"a": "/r"}, unlanded=set())
    assert running == []
    assert steer_check("b", "/r", ["f"], running, {}) is None


def test_steer_check_defers_a_candidate_sharing_a_surface_with_an_unlanded_initiative():
    items = [_item("x", "a", "approved", ["a.rs"]), _item("x", "b", "ready", ["b.rs"])]
    running = running_initiatives(set(), items, {"x": "/r"}, unlanded={"x"})
    assert steer_check("y", "/r", ["a.rs"], running, {}) == {
        "kind": "steer_clear",
        "initiative": "y",
        "other": "x",
        "paths": ["a.rs"],
    }


def test_two_steer_clears_then_a_launch_of_the_initiative_leave_no_key():
    rows = [
        {"ts": "1", "kind": "steer_clear", "initiative": "x", "other": "y"},
        {"ts": "2", "kind": "steer_clear", "initiative": "x", "other": "y"},
        {"ts": "3", "kind": "launch_epic", "initiative": "x"},
    ]
    assert steer_streaks_from_actions(rows) == {}


def test_two_steer_clears_and_no_launch_count_two():
    rows = [
        {"ts": "1", "kind": "steer_clear", "initiative": "x", "other": "y"},
        {"ts": "2", "kind": "steer_clear", "initiative": "x", "other": "y"},
    ]
    assert steer_streaks_from_actions(rows) == {"x|y": 2}


def test_a_store_row_carries_its_action_in_action_json_and_a_launch_of_another_initiative_keeps_the_count():
    rows = [
        {"ts": "2", "kind": "launch_epic", "action_json": '{"kind": "launch_epic", "initiative": "z"}'},
        {"ts": "1", "kind": "steer_clear", "action_json": '{"kind": "steer_clear", "initiative": "x", "other": "y"}'},
    ]
    assert steer_streaks_from_actions(rows) == {"x|y": 1}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _window(spent: float, hours: int) -> pacing.Window:
    return pacing.Window(NOW - timedelta(hours=1), NOW + timedelta(hours=hours - 1), spent, 100.0, 0.0, 0)


def test_a_work_tree_with_two_initiatives_sharing_a_path_gives_steer_check_a_non_empty_overlap(tmp_path):
    _write(tmp_path / "work/x/initiative.md", "---\nid: x\nrepo: /r\n---\n\nbody\n")
    _write(tmp_path / "work/y/initiative.md", "---\nid: y\nrepo: /r\n---\n\nbody\n")
    _write(tmp_path / "work/x/p1/a.md", "---\nid: a\nstate: ready\nsurfaces: [src/a.rs, src/b.rs]\n---\n\nbody\n")
    _write(tmp_path / "work/y/p1/b.md", "---\nid: b\nstate: approved\nsurfaces:\n  - src/b.rs\n---\n\nbody\n")
    _write(tmp_path / "work/y/p1/c.md", "---\nid: c\nstate: ready\nsurfaces: [src/c.rs]\n---\n\nbody\n")
    assert read_initiative_repos(tmp_path, str(tmp_path / "home")) == {"x": "/r", "y": "/r"}
    assert len(read_ticket_items(tmp_path, "files")) == 3
    deps = FactsDeps(
        lease=lambda: {"holder": lease_holder("s", 7, "h"), "host": "h", "epoch": 4, "released": False, "stale": False},
        window=lambda: _window(1.0, 5),
        weekly=lambda: _window(10.0, 168),
        policy=lambda: POLICY,
        docket=lambda: chair_read_docket.read_docket(tmp_path, "files", 2),
        approved=lambda: [],
        quarantined=lambda: [],
        stranded=lambda: [],
        attempts=lambda: [],
        has_patch=lambda initiative, task: False,
        live_initiatives=lambda: ["y"],
        intake=lambda: [],
        work_store_ready=lambda: True,
        sources_configured=lambda: False,
        session="s",
        pid=7,
        host="h",
        idle_stall=lambda n: {
            "free_lanes": 0, "ready": 0, "queued": 0, "last_progress_at": None, "stall_minutes": 15, "hosts": [],
            "empty_stubs": [], "lands_waiting": [], "blocked_ready": [], "open_signature": None, "open_diagnosis": None,
        },
        tickets=lambda: read_ticket_items(tmp_path, "files"),
        repos=lambda: read_initiative_repos(tmp_path, str(tmp_path / "home")),
        actions=lambda: [{"ts": "1", "kind": "steer_clear", "initiative": "x", "other": "y"}],
    )
    facts = gather_facts(deps, NOW)
    (entry,) = facts["initiatives"]
    assert (entry["id"], entry["repo"], entry["ready_surfaces"]) == ("x", "/r", ["src/a.rs", "src/b.rs"])
    assert facts["running"] == [{"id": "y", "repo": "/r", "surfaces": ["src/b.rs", "src/c.rs"], "live": True}]
    assert facts["steer_streaks"] == {"x|y": 1}
    action = steer_check(entry["id"], entry["repo"], entry["ready_surfaces"], facts["running"], facts["steer_streaks"])
    assert action == {"kind": "steer_clear", "initiative": "x", "other": "y", "paths": ["src/b.rs"]}


def test_an_absent_shared_file_limit_is_two():
    assert resolve_shared_file_limit(None) == 2


def test_a_zero_shared_file_limit_is_two():
    assert resolve_shared_file_limit(0) == 2


def test_a_bool_shared_file_limit_is_two():
    assert resolve_shared_file_limit(True) == 2


def test_a_shared_file_limit_below_one_is_two():
    assert resolve_shared_file_limit(0.5) == 2


def test_a_float_shared_file_limit_of_four_is_four():
    assert resolve_shared_file_limit(4.0) == 4


def test_an_infinite_shared_file_limit_is_two():
    assert resolve_shared_file_limit(float("inf")) == 2


def test_a_nan_shared_file_limit_is_two():
    assert resolve_shared_file_limit(float("nan")) == 2


def test_a_non_list_serial_value_is_an_empty_list():
    assert resolve_serial_files("a.py") == []


def test_a_mixed_serial_list_keeps_only_strings():
    assert resolve_serial_files(["a.py", 3, None, "b/"]) == ["a.py", "b/"]


def test_gathered_facts_default_the_steer_policy_to_limit_two_and_no_serial_paths():
    assert gather_facts(_deps(), NOW)["steer_policy"] == {"limit": 2, "serial": []}


def test_gathered_facts_carry_the_steer_policy_from_the_profile_deps():
    deps = replace(_deps(), shared_file_limit=lambda: 4.0, serial_files=lambda: ["x.py"])
    assert gather_facts(deps, NOW)["steer_policy"] == {"limit": 4, "serial": ["x.py"]}
