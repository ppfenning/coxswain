from agent_tools.chair_orchestrate import (
    FALLBACK_NOTICE,
    active_hosts,
    effective_local_lanes,
    mode_line,
    mode_name,
    orchestration_available,
    toggle_refusal,
)
from agent_tools.chair_plan_fill import plan_fill
from agent_tools.chair_types import Facts

NOW = "2026-11-06T12:00:00+00:00"
FRESH = "2026-11-06T11:59:00+00:00"
STALE = "2026-11-06T10:00:00+00:00"


def _row(name: str, state: str = "active", beat_at: str | None = FRESH) -> dict:
    return {"name": name, "state": state, "beat_at": beat_at}


def test_default_any_builds_on_the_chair_machine():
    assert effective_local_lanes("any", ["host-a"]) == "any"
    assert mode_name("any", ["host-a"]) == "building"


def test_toggle_refusal_on_a_one_host_table_is_the_message():
    assert toggle_refusal("on", [_row("chair")], NOW) == "orchestration needs another active lane host"


def test_toggle_refusal_on_a_three_host_table_is_none():
    assert toggle_refusal("on", [_row("chair"), _row("a"), _row("b")], NOW) is None


def test_toggle_refusal_for_off_is_none_on_a_one_host_table():
    assert toggle_refusal("off", [_row("chair")], NOW) is None


def test_a_draining_host_is_not_active():
    assert active_hosts([_row("chair"), _row("a", state="draining")], NOW) == ["chair"]


def test_a_removed_host_is_not_active():
    assert active_hosts([_row("chair"), _row("a", state="removed")], NOW) == ["chair"]


def test_a_stale_beat_host_is_not_active():
    assert active_hosts([_row("chair"), _row("a", beat_at=STALE)], NOW) == ["chair"]


def test_a_host_that_never_beat_is_active():
    assert active_hosts([_row("chair"), _row("a", beat_at=None)], NOW) == ["chair", "a"]


def test_two_active_hosts_make_orchestration_available():
    assert orchestration_available([_row("chair"), _row("a")], NOW) is True


def test_decompose_falls_back_to_any_with_no_lane_hosts():
    assert effective_local_lanes("decompose", []) == "any"
    assert mode_name("decompose", []) == "building"


def test_decompose_returns_when_a_lane_host_is_given():
    assert effective_local_lanes("decompose", ["host-a"]) == "decompose"
    assert mode_name("decompose", ["host-a"]) == "orchestrating"


def test_an_unknown_configured_value_reads_as_any():
    assert effective_local_lanes("bogus", ["host-a"]) == "any"


def test_mode_line_names_the_chair_machine_the_mode_and_availability():
    rows = [_row("chair"), _row("a")]
    assert mode_line("decompose", rows, "chair", NOW) == "chair: orchestrating (available)"
    assert mode_line("decompose", [_row("chair")], "chair", NOW) == "chair: building (unavailable)"


def test_the_fallback_notice_text():
    assert FALLBACK_NOTICE == "orchestration mode on but no other active host; building on the chair machine"


def _init(id: str) -> dict:
    return {"id": id, "started": True, "ready_tasks": [{"id": "t1", "needs": []}], "landed": set()}


def _plan_facts(configured: str, lane_hosts: list[str]) -> Facts:
    dispatch = {
        "max_in_flight": 4,
        "live_runs": 0,
        "hosts": [{"name": name, "live_runs": 0} for name in lane_hosts],
        "local_lanes": effective_local_lanes(configured, lane_hosts),
    }
    return {
        "lease": {"holder": "h", "host": "x", "epoch": 1, "mine": True, "released": False, "stale": False},
        "limits": {"hard_stop": False, "weekly_fraction": 0.0, "hard_stop_fraction": 1.0, "launch_cap": 4, "go_degraded": False},
        "dispatch": dispatch,
        "approved": [],
        "initiatives": [_init("a"), _init("b")],
        "quarantines": [],
        "intake": [],
        "work_store_ready": False,
        "sources_configured": False,
    }


def test_plan_fill_with_one_lane_host_and_decompose_places_every_epic_on_it():
    epics = [a for a in plan_fill(_plan_facts("decompose", ["host-a"]), 2) if a["kind"] == "launch_epic"]
    assert epics and all(a.get("host") == "host-a" for a in epics)


def test_plan_fill_with_the_any_fallback_and_no_lane_host_places_the_epic_locally():
    epics = [a for a in plan_fill(_plan_facts("decompose", []), 1) if a["kind"] == "launch_epic"]
    assert epics == [{"kind": "launch_epic", "initiative": "a"}]
