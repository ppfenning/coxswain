from agent_tools.chair_plan_fill import HostSlot, _epic_launches, _phase_candidates
from agent_tools.chair_types import Facts, InitiativeFacts, RunningInitiative


def _phase(name: str, surfaces: list[str], requires: tuple[str, ...] = ()) -> dict:
    return {
        "phase": name,
        "ready_tasks": [{"id": f"{name}-t", "needs": [], "requires": list(requires)}],
        "surfaces": surfaces,
    }


def _init(id: str, phases: list[dict] | None = None, surfaces: list[str] | None = None) -> InitiativeFacts:
    base: InitiativeFacts = {
        "id": id,
        "started": True,
        "ready_tasks": [{"id": "t1", "needs": [], "requires": []}],
        "landed": set(),
        "repo": "r",
    }
    return {
        **base,
        **({"phases": phases} if phases is not None else {}),
        **({"ready_surfaces": surfaces} if surfaces else {}),
    }


def _facts(initiatives: list[InitiativeFacts], running: list[RunningInitiative] = ()) -> Facts:
    return {"initiatives": initiatives, "running": list(running)}


def _host(name: str, free: int, capabilities: frozenset[str] = frozenset()) -> HostSlot:
    return (name, 1, capabilities, free, 0)


def _live(id: str, phase: str, surfaces: list[str]) -> RunningInitiative:
    return {"id": id, "repo": "r", "surfaces": surfaces, "phase": phase}


TWO = [_phase("p1", ["src/a.rs"]), _phase("p2", ["src/b.rs"])]


def test_candidates_expand_phases_in_order_and_keep_a_phaseless_initiative_whole():
    pairs = [
        (c.initiative["id"], c.phase) for c in _phase_candidates([_init("x", TWO), _init("y", surfaces=["src/c.rs"])])
    ]
    assert pairs == [("x", "p1"), ("x", "p2"), ("y", None)]


def test_two_independent_phases_launch_on_two_hosts_in_one_call():
    actions = _epic_launches(_facts([_init("x", TWO)]), 0, frozenset(), [_host("a", 1), _host("b", 1)])
    assert actions == [
        {"kind": "launch_epic", "initiative": "x", "phase": "p1", "host": "a"},
        {"kind": "launch_epic", "initiative": "x", "phase": "p2", "host": "b"},
    ]


def test_two_phases_sharing_a_file_launch_one_and_steer_the_other():
    shared = [_phase("p1", ["src/a.rs"]), _phase("p2", ["src/a.rs"])]
    actions = _epic_launches(_facts([_init("x", shared)]), 0, frozenset(), [_host("a", 1), _host("b", 1)])
    assert actions == [
        {"kind": "launch_epic", "initiative": "x", "phase": "p1", "host": "a"},
        {
            "kind": "steer_clear",
            "initiative": "x",
            "other": "x",
            "phase": "p2",
            "other_phase": "p1",
            "paths": ["src/a.rs"],
        },
    ]


def test_a_child_phase_launches_while_its_parents_sibling_is_a_live_phase_run():
    child = [_phase("child", ["src/c.rs"])]
    running = [_live("x", "sibling", ["src/b.rs"])]
    actions = _epic_launches(_facts([_init("x", child)], running), 0, frozenset(), [_host("a", 1)])
    assert actions == [{"kind": "launch_epic", "initiative": "x", "phase": "child", "host": "a"}]


def test_a_phase_touching_a_live_sibling_phases_file_is_deferred():
    running = [_live("x", "sibling", ["src/c.rs"])]
    actions = _epic_launches(
        _facts([_init("x", [_phase("child", ["src/c.rs"])])], running), 0, frozenset(), [_host("a", 1)]
    )
    assert actions == [
        {
            "kind": "steer_clear",
            "initiative": "x",
            "other": "x",
            "phase": "child",
            "other_phase": "sibling",
            "paths": ["src/c.rs"],
        }
    ]


def test_an_initiative_without_phase_facts_launches_whole():
    actions = _epic_launches(_facts([_init("x", surfaces=["src/a.rs"])]), 0, frozenset(), [_host("a", 1)])
    assert actions == [{"kind": "launch_epic", "initiative": "x", "host": "a"}]


def test_each_hosts_free_lanes_cap_the_phases_placed():
    three = [*TWO, _phase("p3", ["src/c.rs"])]
    actions = _epic_launches(_facts([_init("x", three)]), 0, frozenset(), [_host("a", 1), _host("b", 1)])
    assert [(a["phase"], a["host"]) for a in actions] == [("p1", "a"), ("p2", "b")]


def test_one_host_with_two_free_lanes_takes_both_phases():
    actions = _epic_launches(_facts([_init("x", TWO)]), 0, frozenset(), [_host("a", 2)])
    assert [(a["phase"], a["host"]) for a in actions] == [("p1", "a"), ("p2", "a")]


def test_local_lanes_fill_before_hosts_across_phases():
    actions = _epic_launches(_facts([_init("x", TWO)]), 1, frozenset(), [_host("a", 1)])
    assert actions == [
        {"kind": "launch_epic", "initiative": "x", "phase": "p1"},
        {"kind": "launch_epic", "initiative": "x", "phase": "p2", "host": "a"},
    ]


def test_a_phase_is_placed_by_its_own_required_capabilities():
    phases = [_phase("p1", ["src/a.rs"], ("gpu",)), _phase("p2", ["src/b.rs"])]
    hosts = [_host("a", 1), _host("b", 1, frozenset({"gpu"}))]
    actions = _epic_launches(_facts([_init("x", phases)]), 0, frozenset(), hosts)
    assert [(a["phase"], a["host"]) for a in actions] == [("p1", "b"), ("p2", "a")]


def test_a_withheld_initiative_launches_none_of_its_phases():
    actions = _epic_launches(_facts([_init("x", TWO)]), 0, frozenset({"x"}), [_host("a", 2)])
    assert actions == []


def test_a_phase_overlapping_a_running_whole_initiative_follows_the_cross_initiative_rule():
    running: list[RunningInitiative] = [{"id": "y", "repo": "r", "surfaces": ["src/a.rs"]}]
    actions = _epic_launches(_facts([_init("x", TWO)], running), 0, frozenset(), [_host("a", 2)])
    assert actions == [
        {"kind": "launch_epic", "initiative": "x", "phase": "p2", "host": "a"},
        {"kind": "steer_clear", "initiative": "x", "other": "y", "phase": "p1", "paths": ["src/a.rs"]},
    ]
