from datetime import UTC, datetime

from agent_tools.chair_plan_fill import plan_fill
from agent_tools.chair_schedule import Placement, Schedule
from agent_tools.chair_types import Facts, InitiativeFacts

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
REMOTE = {"max_in_flight": 5, "live_runs": 0, "hosts": [{"name": "host-a", "live_runs": 0, "capacity": 5}]}
FULL = {"max_in_flight": 5, "live_runs": 0, "hosts": [{"name": "host-a", "live_runs": 5, "capacity": 5}]}


def _init(id: str, started: bool = False, surfaces: list[str] | None = None) -> InitiativeFacts:
    extra = {"repo": "r", "ready_surfaces": surfaces} if surfaces else {}
    return {"id": id, "started": started, "ready_tasks": [{"id": "t1", "needs": [], "requires": []}], "landed": set(), **extra}


def _facts(**over) -> Facts:
    base = {
        "lease": {"holder": "h", "host": "x", "epoch": 1, "mine": True, "released": False, "stale": False},
        "limits": {"hard_stop": False, "weekly_fraction": 0.0, "hard_stop_fraction": 1.0, "launch_cap": 4, "go_degraded": False},
        "dispatch": REMOTE,
        "approved": [],
        "initiatives": [],
        "quarantines": [],
        "intake": [],
        "work_store_ready": False,
        "sources_configured": False,
    }
    return {**base, **over}


def _launch(id: str, **extra) -> dict:
    return {"kind": "launch_epic", "initiative": id, **extra}


def _five() -> Facts:
    ids = [("i1", False), ("i2", False), ("i3", True), ("i4", False), ("i5", False)]
    return _facts(initiatives=[_init(i, started) for i, started in ids])


def _schedule(*ids: str) -> Schedule:
    placed = tuple(Placement((i, "p1"), "host-a", "l1", NOW, NOW, False) for i in ids)
    return Schedule(placed, (), (), None, None, None, (), (), ())


def test_a_budget_of_two_launches_the_two_highest_priority_initiatives_on_remote_lanes():
    assert plan_fill(_five(), 0, launch_budget=2) == [_launch("i3", host="host-a"), _launch("i1", host="host-a")]


def test_a_budget_of_zero_launches_nothing_of_any_kind():
    facts = {**_five(), "intake": ["a.md", "b.md"]}
    assert plan_fill(facts, 0, launch_budget=0) == []
    assert plan_fill(facts, 2, launch_budget=0, hosted_decompose=True) == []


def test_no_budget_equals_the_output_without_the_keyword():
    facts = _five()
    unbounded = plan_fill(facts, 0)
    assert [a["initiative"] for a in unbounded] == ["i3", "i1", "i2", "i4", "i5"]
    assert plan_fill(facts, 0, launch_budget=None) == unbounded


def test_a_budget_larger_than_the_candidates_changes_nothing():
    assert plan_fill(_five(), 0, launch_budget=9) == plan_fill(_five(), 0)


def test_the_budget_covers_local_and_hosted_launches_together():
    assert plan_fill(_five(), 1, launch_budget=1) == [_launch("i3")]
    assert plan_fill(_five(), 1, launch_budget=3) == [_launch("i3"), _launch("i1", host="host-a"), _launch("i2", host="host-a")]


def test_the_scheduled_path_stops_at_the_budget_in_schedule_order():
    facts = {**_five(), "schedule_inputs": {"hosts": [{"name": "host-a", "local": False}]}}
    schedule = _schedule("i5", "i2", "i4")
    assert plan_fill(facts, 0, schedule=schedule, launch_budget=2) == [_launch("i5", host="host-a"), _launch("i2", host="host-a")]
    assert plan_fill(facts, 0, schedule=schedule, launch_budget=0) == []
    assert len(plan_fill(facts, 0, schedule=schedule)) == 3


def test_a_launch_cut_by_the_budget_leaves_no_steer_hold_behind():
    facts = _facts(initiatives=[_init("x", True, ["src/a.rs"]), _init("z", True, ["src/a.rs"])])
    assert plan_fill(facts, 0, launch_budget=1) == [_launch("x", host="host-a")]
    assert [a["kind"] for a in plan_fill(facts, 0)] == ["launch_epic", "steer_clear"]


def test_a_steer_held_candidate_does_not_spend_the_budget():
    facts = _facts(
        initiatives=[_init("x", True, ["src/a.rs"]), _init("z", True, ["src/b.rs"])],
        running=[{"id": "y", "repo": "r", "surfaces": ["src/a.rs"]}],
    )
    kinds = [a["kind"] for a in plan_fill(facts, 0, launch_budget=1)]
    assert kinds == ["launch_epic", "steer_clear"]


def test_decomposes_take_only_the_budget_the_epics_left():
    facts = {**_facts(initiatives=[_init("a", True)]), "intake": ["a.md", "b.md"]}
    assert plan_fill(facts, 3, launch_budget=1) == [_launch("a")]
    both = plan_fill(facts, 3, launch_budget=2)
    assert [a["kind"] for a in both] == ["launch_epic", "launch_decompose"]
    assert len(plan_fill(facts, 3)) == 3


def test_a_steer_hold_beside_intake_does_not_spend_decompose_budget():
    facts = {**_facts(initiatives=[_init("x", True, ["src/a.rs"]), _init("z", True, ["src/a.rs"])]), "intake": ["a.md", "b.md"]}
    assert [a["kind"] for a in plan_fill(facts, 2, launch_budget=2)] == ["launch_epic", "launch_decompose"]
    assert [a["kind"] for a in plan_fill(facts, 2, launch_budget=3)] == ["launch_epic", "steer_clear", "launch_decompose"]


def test_hosted_decomposes_never_push_the_total_past_the_budget_or_ahead_of_epics():
    facts = {**_five(), "intake": ["a.md", "b.md"]}
    out = plan_fill(facts, 1, launch_budget=3, hosted_decompose=True)
    assert [a["kind"] for a in out] == ["launch_epic"] * 3
    out = plan_fill(_facts(dispatch=REMOTE, intake=["a.md", "b.md", "c.md"]), 1, launch_budget=2, hosted_decompose=True)
    assert [a["kind"] for a in out] == ["launch_decompose"] * 2


def test_the_early_return_on_no_free_lanes_still_holds():
    assert plan_fill(_facts(dispatch=FULL, initiatives=[_init("a")]), 0, launch_budget=3) == []
