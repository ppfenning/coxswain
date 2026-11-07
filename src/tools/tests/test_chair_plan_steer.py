from datetime import UTC, datetime

from agent_tools.chair_plan import plan_tick
from agent_tools.chair_types import Facts

_NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


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
        "run_exited": {"x": True},
        "schema_deaths": {},
        "lost_runs": {},
        "last_housekeeping_at": _NOW.isoformat(),
        "housekeeping_hours": 24.0,
        "stale_days": 7,
        "stale_candidates": [],
        "stall_candidates": [],
        "home": {},
    }
    return {**base, **overrides}  # type: ignore[return-value]


def _initiative(id: str, started: bool, surfaces: list[str] | None = None) -> dict:
    entry = {"id": id, "started": started, "ready_tasks": [{"id": f"{id}-t", "needs": []}], "landed": set()}
    return entry if surfaces is None else {**entry, "repo": "r", "ready_surfaces": surfaces}


def _steered(plan: list[dict]) -> list[dict]:
    return [a for a in plan if a["kind"] in {"steer_clear", "needs_chair"}]


def test_a_kept_relaunch_claims_its_surfaces_so_an_overlapping_unstarted_initiative_is_steered_and_a_disjoint_one_launches():
    facts = _facts(
        initiatives=[
            _initiative("x", True, ["a.py"]),
            _initiative("y", False, ["a.py"]),
            _initiative("z", False, ["z.py"]),
        ]
    )
    plan = plan_tick(facts, _NOW)
    assert [a["kind"] for a in plan if a.get("initiative") == "x"] == ["clear_branches", "relaunch"]
    assert [a for a in plan if a.get("initiative") == "y" and a["kind"] != "unlaunched"] == [
        {"kind": "steer_clear", "initiative": "y", "other": "x", "paths": ["a.py"], "epoch": 7}
    ]
    assert [a for a in plan if a["kind"] == "launch_epic"] == [{"kind": "launch_epic", "initiative": "z", "epoch": 7}]


def test_a_steer_clear_takes_no_lane_from_the_launches_after_it():
    # launch_cap 2: the relaunch of x takes one lane, so one remains; y's steer_clear must not take it from z.
    limits = {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 2, "go_degraded": False}
    facts = _facts(
        limits=limits,
        initiatives=[
            _initiative("x", True, ["a.py"]),
            _initiative("y", False, ["a.py"]),
            _initiative("z", False, ["z.py"]),
        ],
    )
    plan = plan_tick(facts, _NOW)
    assert [a["initiative"] for a in plan if a["kind"] == "launch_epic"] == ["z"]


def test_an_initiative_deferred_by_recover_is_not_steered_or_launched_again_by_fill():
    facts = _facts(
        running=[{"id": "w", "repo": "r", "surfaces": ["a.py"]}],
        initiatives=[_initiative("x", True, ["a.py"])],
    )
    plan = plan_tick(facts, _NOW)
    assert _steered(plan) == [{"kind": "steer_clear", "initiative": "x", "other": "w", "paths": ["a.py"], "epoch": 7}]
    assert [a for a in plan if a["kind"] in {"relaunch", "launch_epic", "clear_branches"}] == []


def test_a_deferral_that_reached_the_chair_is_reported_once():
    facts = _facts(
        running=[{"id": "w", "repo": "r", "surfaces": ["a.py"]}],
        steer_streaks={"x|w": 3},
        initiatives=[_initiative("x", True, ["a.py"])],
    )
    plan = plan_tick(facts, _NOW)
    assert [(a["kind"], a["initiative"], a["cause"]) for a in _steered(plan)] == [("needs_chair", "x", "steer_deferred")]


def test_facts_without_running_streaks_or_repo_plan_as_before():
    facts = _facts(initiatives=[_initiative("x", True), _initiative("z", False)])
    assert plan_tick(facts, _NOW) == [
        {"kind": "clear_branches", "initiative": "x", "epoch": 7},
        {"kind": "relaunch", "initiative": "x", "epoch": 7},
        {"kind": "launch_epic", "initiative": "z", "epoch": 7},
    ]


def _stuck(plan: list[dict]) -> list[dict]:
    return [a for a in plan if a.get("cause") == "steer_head_stuck"]


def test_a_head_holding_a_file_launches_or_b_is_not_deferred_whichever_order_the_docket_lists_them():
    # a holds f, is idle with ready tasks and no run; b wants f.
    for order in (("a", "b"), ("b", "a")):
        plan = plan_tick(_facts(initiatives=[_initiative(i, False, ["f"]) for i in order]), _NOW)
        launched = {a["initiative"] for a in plan if a["kind"] == "launch_epic"}
        b_deferred = any(a["kind"] == "steer_clear" and a["initiative"] == "b" for a in plan)
        assert not b_deferred or "a" in launched
        assert _stuck(plan) == []


def test_a_head_withheld_from_the_launch_list_is_reported_stuck_with_the_withhold_named():
    # a is started and its last run has not exited, so its relaunch is withheld but still claims f; b is deferred behind it.
    facts = _facts(run_exited={"a": False}, initiatives=[_initiative("a", True, ["f"]), _initiative("b", False, ["f"])])
    plan = plan_tick(facts, _NOW)
    assert [a["kind"] for a in plan if a.get("initiative") == "a" and a["kind"] != "needs_chair"] == []
    assert [(a["kind"], a["initiative"]) for a in plan if a["kind"] == "steer_clear"] == [("steer_clear", "b")]
    assert _stuck(plan) == [
        {"kind": "needs_chair", "initiative": "a", "cause": "steer_head_stuck", "reason": "launch refused: its last run has not exited", "epoch": 7}
    ]


def test_a_head_no_lane_or_host_can_take_is_reported_stuck_as_no_host():
    # Local lanes are decompose-only and the one lane host lacks the "go" a needs; c takes the host's last slot after b is deferred.
    dispatch = {"max_in_flight": 4, "live_runs": 0, "local_lanes": "decompose", "hosts": [{"name": "h1", "capacity": 1, "live_runs": 0}]}
    a = {**_initiative("a", True, ["f"]), "ready_tasks": [{"id": "a-t", "needs": [], "requires": ["go"]}]}
    facts = _facts(
        dispatch=dispatch,
        home={"a": ""},
        run_exited={"a": True},
        initiatives=[a, _initiative("b", False, ["f"]), _initiative("c", False, ["c.py"])],
    )
    plan = plan_tick(facts, _NOW)
    assert [(x["kind"], x["initiative"]) for x in plan if x["kind"] == "launch_epic"] == [("launch_epic", "c")]
    assert _stuck(plan) == [{"kind": "needs_chair", "initiative": "a", "cause": "steer_head_stuck", "reason": "no host", "epoch": 7}]


def test_a_head_that_launches_gets_no_stuck_action_and_the_hard_stop_gets_none_either():
    live = _facts(run_exited={"a": True}, initiatives=[_initiative("a", True, ["f"]), _initiative("b", False, ["f"])])
    assert [a["kind"] for a in plan_tick(live, _NOW) if a.get("initiative") == "a"] == ["clear_branches", "relaunch"]
    assert _stuck(plan_tick(live, _NOW)) == []
    limits = {"hard_stop": True, "weekly_fraction": 0.95, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False}
    stopped = _facts(limits=limits, run_exited={"a": False}, initiatives=[_initiative("a", True, ["f"]), _initiative("b", False, ["f"])])
    assert _stuck(plan_tick(stopped, _NOW)) == []
