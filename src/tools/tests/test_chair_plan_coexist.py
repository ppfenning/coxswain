"""Lands, decomposes and builds run side by side: a land withholds only what it lands, and a decompose on the
chair machine's lane leaves the lane hosts' room for builds."""

from datetime import UTC, datetime

from agent_tools.chair_plan import plan_tick
from agent_tools.chair_plan_recover import plan_recover
from agent_tools.chair_types import Facts

_NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


def _facts(**overrides) -> Facts:
    base = {
        "lease": {"holder": "a", "host": "h", "epoch": 7, "mine": True, "released": False, "stale": False},
        "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False},
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


def _initiative(id: str, repo: str = "r", started: bool = False) -> dict:
    return {"id": id, "repo": repo, "started": started, "ready_tasks": [{"id": f"{id}-t", "needs": []}], "landed": set()}


def _launches(actions: list[dict]) -> list[tuple[str, str, str]]:
    return [(a["kind"], a["initiative"], a.get("host", "")) for a in actions if a["kind"] in {"launch_epic", "relaunch"}]


def _withheld_why(actions: list[dict], initiative: str) -> list[str]:
    return [a["reason"] for a in actions if a.get("initiative") == initiative and "reason" in a]


_LAND_A = [{"initiative": "a", "phase": "p1", "repo": "r"}]


def test_a_land_in_flight_on_one_initiative_leaves_another_in_its_repo_launchable():
    facts = _facts(initiatives=[_initiative("a"), _initiative("b")], landing=_LAND_A)
    assert _launches(plan_tick(facts, _NOW)) == [("launch_epic", "b", "")]


def test_the_landed_initiative_alone_is_withheld_because_its_repo_is_landing():
    actions = plan_tick(_facts(initiatives=[_initiative("a"), _initiative("b")], landing=_LAND_A), _NOW)
    assert any("its repo is landing" in why for why in _withheld_why(actions, "a"))
    assert not any("its repo is landing" in why for why in _withheld_why(actions, "b"))


def test_a_land_naming_no_initiative_still_withholds_its_whole_repo():
    landing = [{"initiative": "", "phase": "", "repo": "r"}]
    facts = _facts(initiatives=[_initiative("a"), _initiative("b"), _initiative("c", repo="r2")], landing=landing)
    assert _launches(plan_tick(facts, _NOW)) == [("launch_epic", "c", "")]


def test_a_land_in_flight_on_one_initiative_still_relaunches_another_in_its_repo():
    facts = _facts(initiatives=[_initiative("a", started=True), _initiative("b", started=True)], landing=_LAND_A)
    assert plan_recover(facts) == [{"kind": "clear_branches", "initiative": "b"}, {"kind": "relaunch", "initiative": "b"}]


def _decompose_live(hosts: list[dict]) -> dict:
    """The chair machine's one lane runs a decompose; its local lanes are kept for decomposes."""
    return {"max_in_flight": 1, "live_runs": 1, "local_lanes": "decompose", "hosts": hosts}


def test_a_decompose_live_on_the_chair_machine_leaves_a_ready_build_its_remote_lane():
    facts = _facts(
        limits={"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 1, "go_degraded": False},
        dispatch=_decompose_live([{"name": "omarchy", "capacity": 3, "live_runs": 0}]),
        initiatives=[_initiative("b")],
    )
    assert _launches(plan_tick(facts, _NOW)) == [("launch_epic", "b", "omarchy")]


def test_a_decompose_live_on_the_chair_machine_leaves_a_relaunch_its_remote_lane():
    facts = _facts(
        limits={"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 1, "go_degraded": False},
        dispatch=_decompose_live([{"name": "omarchy", "capacity": 3, "live_runs": 0}]),
        initiatives=[_initiative("a", started=True), _initiative("b")],
        run_exited={"a": True},
    )
    assert _launches(plan_tick(facts, _NOW)) == [("relaunch", "a", "omarchy"), ("launch_epic", "b", "omarchy")]


def test_the_chair_machine_room_still_bounds_what_launches_on_it():
    facts = _facts(
        dispatch={"max_in_flight": 2, "live_runs": 1, "hosts": []},
        initiatives=[_initiative("a", repo="r1"), _initiative("b", repo="r2"), _initiative("c", repo="r3")],
    )
    assert _launches(plan_tick(facts, _NOW)) == [("launch_epic", "a", "")]


def test_the_weekly_launch_cap_still_limits_launches():
    facts = _facts(
        limits={"hard_stop": False, "weekly_fraction": 0.5, "hard_stop_fraction": 0.9, "launch_cap": 1, "go_degraded": False},
        dispatch={"max_in_flight": 5, "live_runs": 0, "hosts": []},
        initiatives=[_initiative("a", repo="r1"), _initiative("b", repo="r2"), _initiative("c", repo="r3")],
    )
    assert _launches(plan_tick(facts, _NOW)) == [("launch_epic", "a", "")]


def test_the_weekly_lane_fraction_still_limits_remote_launches():
    facts = _facts(
        limits={
            "hard_stop": False, "weekly_fraction": 0.5, "hard_stop_fraction": 0.9, "launch_cap": 1, "go_degraded": False,
            "lane_fraction": 0.5,
        },
        dispatch=_decompose_live([{"name": "omarchy", "capacity": 4, "live_runs": 0}]),
        initiatives=[_initiative(i, repo=f"r{i}") for i in "abcd"],
    )
    assert _launches(plan_tick(facts, _NOW)) == [("launch_epic", "a", "omarchy"), ("launch_epic", "b", "omarchy")]
