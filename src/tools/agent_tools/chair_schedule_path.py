"""Weighted critical paths over a node graph, as plain data in and plain data out.

`dependents` maps a node key to the keys that wait on it. A node's value is its own weight plus the
largest value among its dependents, so the value says how much work is stuck behind it.
Keys must be hashable and mutually sortable: ties resolve by sorted key.
"""

from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class PathResult:
    """A cycle leaves `values` and `chain` empty and names the cycle's keys in `cycle`.

    `unweighted` lists the sorted keys that had no weight and were counted as 0.
    """

    values: dict
    chain: tuple
    cycle: tuple
    unweighted: tuple = ()


def _nodes(weights: Mapping, dependents: Mapping[Hashable, Iterable]) -> list:
    named = set(weights) | set(dependents) | {d for ds in dependents.values() for d in ds}
    return sorted(named)


def _next(dependents: Mapping[Hashable, Iterable], key) -> list:
    return sorted(set(dependents.get(key, ())))


def find_cycle(weights: Mapping, dependents: Mapping[Hashable, Iterable]) -> tuple:
    """Keys of one cycle, rotated to start at its smallest key, or () when the graph is acyclic."""
    # Iterative three-colour walk: the mutation is local to this function and avoids the recursion limit.
    colour: dict = {}
    for root in _nodes(weights, dependents):
        if colour.get(root):
            continue
        colour[root] = 1
        path = [root]
        walks = [iter(_next(dependents, root))]
        while path:
            for nxt in walks[-1]:
                seen = colour.get(nxt, 0)
                if seen == 1:
                    loop = path[path.index(nxt) :]
                    start = loop.index(min(loop))
                    return tuple(loop[start:] + loop[:start])
                if seen == 0:
                    colour[nxt] = 1
                    path.append(nxt)
                    walks.append(iter(_next(dependents, nxt)))
                    break
            else:
                colour[path.pop()] = 2
                walks.pop()
    return ()


def critical_values(weights: Mapping, dependents: Mapping[Hashable, Iterable]) -> dict:
    """Own weight plus the largest dependent value. A key with no weight counts as 0. Acyclic input only."""
    nodes = _nodes(weights, dependents)
    waiting = {n: len(_next(dependents, n)) for n in nodes}
    blockers: dict = {n: [] for n in nodes}
    for n in nodes:
        for d in _next(dependents, n):
            blockers[d].append(n)
    values: dict = {}
    ready = [n for n in nodes if waiting[n] == 0]
    while ready:
        n = ready.pop()
        values[n] = weights.get(n, 0) + max((values[d] for d in _next(dependents, n)), default=0)
        for b in blockers[n]:
            waiting[b] -= 1
            if waiting[b] == 0:
                ready.append(b)
    return values


def longest_chain(values: Mapping, dependents: Mapping[Hashable, Iterable]) -> tuple:
    """The chain from the highest-value root, stepping to the highest-value dependent; ties take the smallest key.

    The start is a root, a node no other node precedes, so a zero-weight blocker is never dropped from the head.
    """
    waited_on = {d for ds in dependents.values() for d in ds}
    roots = [k for k in values if k not in waited_on]
    if not roots:
        return ()
    chain = [min(roots, key=lambda k: (-values[k], k))]
    while True:
        options = [d for d in _next(dependents, chain[-1]) if d in values]
        if not options:
            return tuple(chain)
        chain.append(min(options, key=lambda k: (-values[k], k)))


def critical_paths(weights: Mapping, dependents: Mapping[Hashable, Iterable]) -> PathResult:
    unweighted = tuple(k for k in _nodes(weights, dependents) if k not in weights)
    cycle = find_cycle(weights, dependents)
    if cycle:
        return PathResult(values={}, chain=(), cycle=cycle, unweighted=unweighted)
    values = critical_values(weights, dependents)
    return PathResult(values=values, chain=longest_chain(values, dependents), cycle=(), unweighted=unweighted)
