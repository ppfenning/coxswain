"""The launch cap bounds every launch the tick plans, on the chair machine and on the lane hosts alike."""

from datetime import UTC, datetime

from agent_tools.chair_facts import fleet_lanes
from agent_tools.chair_plan import plan_tick
from agent_tools.chair_types import Facts

_NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
_LAUNCH_KINDS = {"launch_epic", "launch_decompose", "relaunch", "retry", "rescue"}


def _limits(cap: int, **extra) -> dict:
    return {"hard_stop": False, "weekly_fraction": 0.5, "hard_stop_fraction": 0.9, "launch_cap": cap, "go_degraded": False, **extra}


def _hosts(*names: str, capacity: int = 3) -> list[dict]:
    return [{"name": name, "capacity": capacity, "live_runs": 0} for name in names]


def _facts(**overrides) -> Facts:
    base = {
        "lease": {"holder": "a", "host": "h", "epoch": 7, "mine": True, "released": False, "stale": False},
        "limits": _limits(5),
        "dispatch": {"max_in_flight": 1, "live_runs": 1, "local_lanes": "decompose", "hosts": _hosts("lane-b", "friday")},
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


def _initiative(id: str, started: bool = False) -> dict:
    return {"id": id, "repo": f"r-{id}", "started": started, "ready_tasks": [{"id": f"{id}-t", "needs": []}], "landed": set()}


def _launched(actions: list[dict]) -> list[str]:
    return [a["initiative"] for a in actions if a["kind"] in _LAUNCH_KINDS]


def _unlaunched(actions: list[dict]) -> dict[str, str]:
    return {a["initiative"]: a["reason"] for a in actions if a["kind"] == "unlaunched"}


_LANDABLE = [
    {"id": "t1", "initiative": "z", "repo": "r-z", "phase": "p", "phase_done": True, "needs": [], "run": "z-1", "needs_fetch": False}
]


def _capped_at_zero(**limits) -> list[dict]:
    facts = _facts(
        limits=_limits(0, **limits),
        initiatives=[_initiative("a", started=True), _initiative("b"), _initiative("c")],
        run_exited={"a": True},
        approved=_LANDABLE,
        remote_unfetched={"y": "y-3"},
    )
    return plan_tick(facts, _NOW)


def test_a_launch_cap_of_zero_with_free_remote_lanes_plans_no_launch_but_still_lands_and_fetches():
    actions = _capped_at_zero()
    assert _launched(actions) == []
    assert [a["initiative"] for a in actions if a["kind"] == "land_phase"] == ["z"]
    assert [(a["initiative"], a["run"]) for a in actions if a["kind"] == "fetch_exit"] == [("y", "y-3")]


def test_a_go_degraded_verdict_launches_nothing_on_the_lane_hosts():
    assert _launched(_capped_at_zero(go_degraded=True)) == []


def test_a_smoke_hold_launches_nothing_on_the_lane_hosts():
    hold = {"land": {"repo": "r", "pr": 1, "commit": "abc"}, "failing_command": ["pytest"], "tail": "boom", "cause": "smoke_failed"}
    assert _launched(_capped_at_zero(smoke_hold=hold)) == []


def test_each_initiative_the_zero_cap_holds_back_is_named_as_paced_whatever_host_it_would_use():
    unlaunched = _unlaunched(_capped_at_zero())
    assert unlaunched == dict.fromkeys(("a", "b", "c"), "paced against the weekly target")


def test_a_launch_cap_of_two_with_five_ready_initiatives_launches_the_two_first_in_priority_order():
    facts = _facts(limits=_limits(2), initiatives=[_initiative(i) for i in "abcde"])
    actions = plan_tick(facts, _NOW)
    assert _launched(actions) == ["a", "b"]
    assert all(a.get("host") in {"lane-b", "friday"} for a in actions if a["kind"] == "launch_epic")
    assert _unlaunched(actions) == dict.fromkeys("cde", "paced against the weekly target")


def test_a_relaunch_counts_against_the_cap_ahead_of_a_fresh_launch():
    facts = _facts(limits=_limits(1), initiatives=[_initiative("a", started=True), _initiative("b")], run_exited={"a": True})
    actions = plan_tick(facts, _NOW)
    assert _launched(actions) == ["a"]
    assert [a["kind"] for a in actions if a.get("initiative") == "a" and a["kind"] in {"clear_branches", "relaunch"}] == [
        "clear_branches",
        "relaunch",
    ]


def test_relaunches_past_the_cap_drop_with_their_paired_clear_branches():
    facts = _facts(
        limits=_limits(1),
        initiatives=[_initiative("a", started=True), _initiative("b", started=True)],
        run_exited={"a": True, "b": True},
    )
    actions = plan_tick(facts, _NOW)
    assert _launched(actions) == ["a"]
    assert [a["initiative"] for a in actions if a["kind"] == "clear_branches"] == ["a"]


def test_a_cap_above_the_free_lanes_launches_on_every_free_lane():
    facts = _facts(limits=_limits(9), initiatives=[_initiative(i) for i in "abcdefgh"])
    assert len(_launched(plan_tick(facts, _NOW))) == 6


def test_the_fleet_lanes_count_the_chair_machine_and_every_lane_host():
    dispatch = {"max_in_flight": 1, "live_runs": 0, "hosts": [{"name": "lane-b", "capacity": 4}, {"name": "friday"}]}
    assert fleet_lanes(dispatch) == 1 + 4 + 1
