import dataclasses

import pytest

from agent_tools.chair_schedule_graph import Initiative, Phase, build_graph


def _ini(id: str, priority: int, *phases: Phase, repo: str = "r") -> Initiative:
    return Initiative(id, priority, repo, phases)


def _ph(id: str, status: str = "ready", surfaces: tuple[str, ...] = (), parents: tuple[str, ...] = ()) -> Phase:
    return Phase(id, status, parents, "S", surfaces)


def _acyclic(edges: dict) -> bool:
    left = {k: set(v) for k, v in edges.items()}
    while left:
        free = [k for k in left if not any(k in v for v in left.values())]
        if not free:
            return False
        left = {k: v for k, v in left.items() if k not in free}
    return True


def test_parent_edge_within_initiative():
    _, edges = build_graph([_ini("a", 1, _ph("p1"), _ph("p2", "waiting", parents=("p1",)))])
    assert edges == {("a", "p1"): frozenset({("a", "p2")}), ("a", "p2"): frozenset()}


def test_complete_phase_is_no_node_and_no_edge():
    nodes, edges = build_graph([_ini("a", 1, _ph("p1", "complete"), _ph("p2", parents=("p1",)))])
    assert list(nodes) == [("a", "p2")]
    assert edges == {("a", "p2"): frozenset()}


def test_node_is_frozen():
    nodes, _ = build_graph([_ini("a", 1, _ph("p1"))])
    with pytest.raises(dataclasses.FrozenInstanceError):
        nodes[("a", "p1")].status = "running"  # type: ignore[misc]


def test_strict_conflict_edge_follows_priority():
    _, edges = build_graph([_ini("a", 1, _ph("p", surfaces=("x.py",))), _ini("b", 5, _ph("q", surfaces=("x.py",)))])
    assert edges[("b", "q")] == frozenset({("a", "p")})
    assert edges[("a", "p")] == frozenset()


def test_other_repo_gives_no_edge():
    _, edges = build_graph(
        [_ini("a", 1, _ph("p", surfaces=("x.py",))), _ini("b", 1, _ph("q", surfaces=("x.py",)), repo="other")]
    )
    assert edges == {("a", "p"): frozenset(), ("b", "q"): frozenset()}


def test_shareable_file_gives_no_edge():
    snap = ("src/ui/snapshots/a.snap",)
    _, edges = build_graph([_ini("a", 1, _ph("p", surfaces=snap)), _ini("b", 1, _ph("q", surfaces=snap))])
    assert edges == {("a", "p"): frozenset(), ("b", "q"): frozenset()}


def test_running_phase_wins_at_equal_priority():
    _, edges = build_graph(
        [_ini("a", 1, _ph("p", surfaces=("x.py",))), _ini("b", 1, _ph("q", "running", surfaces=("x.py",)))]
    )
    assert edges[("b", "q")] == frozenset({("a", "p")})


def test_priority_beats_running():
    _, edges = build_graph(
        [_ini("a", 9, _ph("p", surfaces=("x.py",))), _ini("b", 1, _ph("q", "running", surfaces=("x.py",)))]
    )
    assert edges[("a", "p")] == frozenset({("b", "q")})


def test_queue_order_breaks_a_full_tie():
    _, edges = build_graph([_ini("a", 1, _ph("p", surfaces=("x.py",))), _ini("b", 1, _ph("q", surfaces=("x.py",)))])
    assert edges[("a", "p")] == frozenset({("b", "q")})


def test_three_initiatives_sharing_one_file_have_no_cycle():
    ph = _ph("p", surfaces=("x.py",))
    nodes, edges = build_graph([_ini("a", 1, ph), _ini("b", 3, ph), _ini("c", 2, _ph("p", "running", ("x.py",)))])
    assert sum(len(v) for v in edges.values()) == 3
    assert _acyclic(edges)
    assert set(edges) == set(nodes)


def test_blocked_later_phase_holds_no_edge():
    a = _ini("a", 5, _ph("p1"), _ph("p2", "blocked", surfaces=("cli.py",), parents=("p1",)))
    _, edges = build_graph([a, _ini("b", 1, _ph("q", surfaces=("cli.py",)))])
    assert edges[("a", "p2")] == frozenset()
    assert edges[("b", "q")] == frozenset()


def test_waiting_phase_holds_no_edge():
    _, edges = build_graph(
        [_ini("a", 5, _ph("p", "waiting", surfaces=("cli.py",))), _ini("b", 1, _ph("q", surfaces=("cli.py",)))]
    )
    assert edges[("a", "p")] == frozenset()
    assert edges[("b", "q")] == frozenset()


def test_running_phase_keeps_its_edge():
    _, edges = build_graph(
        [_ini("a", 5, _ph("p", "running", surfaces=("cli.py",))), _ini("b", 1, _ph("q", surfaces=("cli.py",)))]
    )
    assert edges[("a", "p")] == frozenset({("b", "q")})


def test_withheld_initiative_holds_no_edge():
    _, edges = build_graph(
        [_ini("a", 5, _ph("p", surfaces=("cli.py",))), _ini("b", 1, _ph("q", surfaces=("cli.py",)))],
        withheld=frozenset({"a"}),
    )
    assert edges[("a", "p")] == frozenset()
    assert edges[("b", "q")] == frozenset()


def test_two_ready_phases_still_get_an_edge():
    _, edges = build_graph(
        [_ini("a", 5, _ph("p", surfaces=("cli.py",))), _ini("b", 1, _ph("q", surfaces=("cli.py",)))],
        withheld=frozenset(),
    )
    assert edges[("a", "p")] == frozenset({("b", "q")})


def test_ready_phase_behind_a_waiting_parent_holds_no_edge():
    _, edges = build_graph(
        [
            _ini("x", 5, _ph("p1", "waiting"), _ph("p2", surfaces=("cli.py",), parents=("p1",))),
            _ini("y", 1, _ph("q", surfaces=("cli.py",))),
        ]
    )
    assert edges[("x", "p2")] == frozenset()


def test_ready_phase_behind_a_waiting_grandparent_holds_no_edge():
    _, edges = build_graph(
        [
            _ini(
                "x",
                5,
                _ph("p1", "waiting"),
                _ph("p2", parents=("p1",)),
                _ph("p3", surfaces=("cli.py",), parents=("p2",)),
            ),
            _ini("y", 1, _ph("q", surfaces=("cli.py",))),
        ]
    )
    assert edges[("x", "p3")] == frozenset()


def test_ready_phase_behind_a_ready_parent_keeps_its_edge():
    _, edges = build_graph(
        [
            _ini("x", 5, _ph("p1"), _ph("p2", surfaces=("cli.py",), parents=("p1",))),
            _ini("y", 1, _ph("q", surfaces=("cli.py",))),
        ]
    )
    assert edges[("x", "p2")] == frozenset({("y", "q")})
