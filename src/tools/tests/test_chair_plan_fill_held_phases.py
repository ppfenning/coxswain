from agent_tools.chair_plan_fill import plan_fill
from agent_tools.chair_types import Facts, InitiativeFacts

P1_HELD = frozenset({("x", "p1")})


def _task(id: str, phase: str, requires: tuple[str, ...] = ()) -> dict:
    return {"id": id, "phase": phase, "needs": [], "requires": list(requires)}


def _init(id: str, tasks: list[dict], surfaces: list[str] | None = None) -> InitiativeFacts:
    extra = {"repo": "r", "ready_surfaces": surfaces} if surfaces else {}
    return {"id": id, "started": True, "ready_tasks": tasks, "landed": set(), **extra}


def _facts(**over) -> Facts:
    base = {
        "lease": {"holder": "h", "host": "x", "epoch": 1, "mine": True, "released": False, "stale": False},
        "limits": {
            "hard_stop": False,
            "weekly_fraction": 0.0,
            "hard_stop_fraction": 1.0,
            "launch_cap": 4,
            "go_degraded": False,
        },
        "dispatch": {
            "max_in_flight": 4,
            "live_runs": 0,
            "hosts": [{"name": "host-a", "live_runs": 3, "capabilities": []}],
        },
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


def _p1_needs_gpu() -> list[dict]:
    return [_task("a", "p1", ("gpu",)), _task("b", "p3")]


def test_a_held_phase_is_skipped_and_the_initiative_launches_for_the_other_phase():
    facts = _facts(initiatives=[_init("x", _p1_needs_gpu())])
    assert plan_fill(facts, 0, held_phases=P1_HELD) == [_launch("x", host="host-a")]


def test_an_empty_held_phases_behaves_as_before():
    facts = _facts(initiatives=[_init("x", _p1_needs_gpu())])
    assert plan_fill(facts, 0, held_phases=frozenset()) == plan_fill(facts, 0) == []


def test_an_initiative_with_only_held_ready_work_launches_nothing():
    facts = _facts(initiatives=[_init("x", [_task("a", "p1")]), _init("y", [_task("c", "p1")])])
    assert plan_fill(facts, 1, held_phases=P1_HELD) == [_launch("y")]


def test_the_phase_is_held_for_its_own_initiative_only():
    facts = _facts(initiatives=[_init("y", [_task("c", "p1")])])
    assert plan_fill(facts, 1, held_phases=P1_HELD) == [_launch("y")]


def test_held_work_still_counts_for_steer_overlap():
    facts = _facts(
        initiatives=[_init("x", [_task("a", "p1"), _task("b", "p3")], ["src/main.rs"])],
        running=[{"id": "y", "repo": "r", "surfaces": ["src/main.rs"]}],
    )
    assert plan_fill(facts, 1, held_phases=P1_HELD) == [
        {"kind": "steer_clear", "initiative": "x", "other": "y", "paths": ["src/main.rs"]}
    ]
