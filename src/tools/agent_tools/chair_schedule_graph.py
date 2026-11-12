"""Pure core: plain initiatives in, one graph of all open work out; no clock, filesystem or process."""
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import Literal

from agent_tools.chair_steer import shared_paths

Status = Literal["ready", "waiting", "blocked", "running", "complete"]
Key = tuple[str, str]  # (initiative id, phase id)
Edge = tuple[Key, Key]  # (first, second): second waits for first
Rank = tuple[int, int, int]

_OPEN = frozenset({"ready", "waiting", "blocked", "running"})


@dataclass(frozen=True)
class Phase:
    id: str
    status: Status
    parents: tuple[str, ...] = ()
    size: str = ""
    surfaces: tuple[str, ...] = ()


@dataclass(frozen=True)
class Initiative:
    id: str
    priority: int  # higher runs first
    repo: str
    phases: tuple[Phase, ...]


@dataclass(frozen=True)
class Node:
    initiative: str
    phase: str
    status: Status
    size: str
    surfaces: tuple[str, ...]
    priority: int
    repo: str

    @property
    def key(self) -> Key:
        return (self.initiative, self.phase)


def build_nodes(initiatives: Sequence[Initiative]) -> dict[Key, Node]:
    """One node per phase that is not complete."""
    nodes = (
        Node(i.id, p.id, p.status, p.size, p.surfaces, i.priority, i.repo)
        for i in initiatives
        for p in i.phases
        if p.status in _OPEN
    )
    return {n.key: n for n in nodes}


def parent_edges(initiatives: Sequence[Initiative], nodes: Mapping[Key, Node]) -> frozenset[Edge]:
    """(parent, child) inside one initiative; a parent that is not a node, such as a complete one, adds none."""
    return frozenset(
        ((i.id, parent), (i.id, p.id))
        for i in initiatives
        for p in i.phases
        if (i.id, p.id) in nodes
        for parent in p.parents
        if (i.id, parent) in nodes
    )


def _ranks(initiatives: Sequence[Initiative]) -> dict[str, Rank]:
    """Lower sorts first: higher priority, then an initiative with a running phase, then queue position.

    Running is read per initiative, not per phase, so every conflict edge follows one order on
    initiatives and no cycle can cross initiatives.
    """
    return {
        i.id: (-i.priority, 0 if any(p.status == "running" for p in i.phases) else 1, position)
        for position, i in enumerate(initiatives)
    }


def _ordered(a: Node, b: Node, ranks: Mapping[str, Rank]) -> tuple[Node, Node]:
    return (a, b) if ranks[a.initiative] <= ranks[b.initiative] else (b, a)


def can_start(node: Node, withheld: frozenset[str]) -> bool:
    """Running, or ready and not withheld; blocked, waiting and withheld nodes hold no surfaces."""
    return node.status == "running" or (node.status == "ready" and node.initiative not in withheld)


def placeable(
    initiatives: Sequence[Initiative], nodes: Mapping[Key, Node], withheld: frozenset[str] = frozenset()
) -> frozenset[Key]:
    """The node keys that can start once their own initiative's parents finish.

    A node is placeable when it can start and every parent that is still a node is placeable too, so a ready
    phase behind a waiting, blocked or withheld one is not: the planner never places it.
    """
    parents = {(i.id, p.id): tuple((i.id, q) for q in p.parents if (i.id, q) in nodes) for i in initiatives for p in i.phases}
    memo: dict[Key, bool] = {}

    def ok(key: Key, seen: frozenset[Key]) -> bool:
        if key not in memo:
            memo[key] = key not in seen and can_start(nodes[key], withheld) and all(
                ok(q, seen | {key}) for q in parents.get(key, ())
            )
        return memo[key]

    return frozenset(k for k in nodes if ok(k, frozenset()))


def conflict_edges(
    initiatives: Sequence[Initiative],
    nodes: Mapping[Key, Node],
    *,
    withheld: frozenset[str] = frozenset(),
) -> frozenset[Edge]:
    """One edge per pair of nodes in different initiatives of one repo whose surfaces overlap strictly.

    The first node of the pair, the one the other waits behind, must be placeable: one behind a waiting, blocked or
    withheld phase of its own initiative never places, so an edge from it would leave the second unplaced for good.
    """
    ranks = _ranks(initiatives)
    starts = placeable(initiatives, nodes, withheld)
    ordered = (
        _ordered(a, b, ranks)
        for a, b in combinations(nodes.values(), 2)
        if a.initiative != b.initiative and a.repo == b.repo and shared_paths(a.surfaces, b.surfaces)
    )
    return frozenset((first.key, second.key) for first, second in ordered if first.key in starts)


def build_graph(
    initiatives: Sequence[Initiative], *, withheld: frozenset[str] = frozenset()
) -> tuple[dict[Key, Node], dict[Key, frozenset[Key]]]:
    """The nodes, and for each node key the keys that wait for it; every node is a key."""
    nodes = build_nodes(initiatives)
    edges = parent_edges(initiatives, nodes) | conflict_edges(initiatives, nodes, withheld=withheld)
    return nodes, {k: frozenset(after for before, after in edges if before == k) for k in nodes}
