"""Phase waves: which phases can run side by side, derived from the phase graph. Pure, no I/O."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def phase_parents(items: Sequence[Mapping[str, Any]]) -> dict[str, set[str]]:
    """Phase B has parent A when a task in B needs a task in A that is not done; a done need adds none."""
    phase_of = {str(item["id"]): str(item.get("phase") or "") for item in items}
    state_of = {str(item["id"]): item.get("state") for item in items}
    parents: dict[str, set[str]] = {phase: set() for phase in phase_of.values() if phase}
    for item in items:
        here = str(item.get("phase") or "")
        for need in item.get("needs") or []:
            there = phase_of.get(str(need))
            if here and there and there != here and state_of.get(str(need)) != "done":
                parents[here].add(there)
    return parents


def compute_waves(parents: Mapping[str, set[str]]) -> tuple[list[list[str]], list[str]]:
    """Wave n holds the phases whose parents all sit in earlier waves; phases in a cycle are returned apart."""
    waves: list[list[str]] = []
    placed: set[str] = set()
    while True:
        ready = sorted(p for p in parents if p not in placed and parents[p] <= placed)
        if not ready:
            return waves, sorted(set(parents) - placed)
        waves = [*waves, ready]
        placed = placed | set(ready)


def widest_wave(waves: Sequence[Sequence[str]]) -> int:
    """Size of the largest wave; 0 when there are none."""
    return max(map(len, waves), default=0)


def phase_ancestors(parents: Mapping[str, set[str]]) -> dict[str, set[str]]:
    """Every phase a phase inherits, directly or through parents; a cycle ends the walk."""

    def walk(phase: str) -> set[str]:
        seen: set[str] = set()
        stack = list(parents.get(phase, ()))
        while stack:
            here = stack.pop()
            if here not in seen:
                seen = seen | {here}
                stack = [*stack, *parents.get(here, ())]
        return seen

    return {phase: walk(phase) for phase in parents}


def _surface_files(item: Mapping[str, Any]) -> set[str]:
    return {str(surface).removesuffix(" (new)") for surface in item.get("surfaces") or []}


def unreached_phases(items: Sequence[Mapping[str, Any]], phase_order: Sequence[str]) -> dict[str, dict[str, list[str]]]:
    """Later phase to each earlier phase it does not inherit, with the files both name in surfaces."""
    ancestors = phase_ancestors(phase_parents(items))
    in_order = set(phase_order)
    files = {
        phase: set().union(*(_surface_files(i) for i in items if str(i.get("phase") or "") == phase))
        for phase in in_order
    }
    done = {
        phase
        for phase in in_order
        if (states := [i.get("state") for i in items if str(i.get("phase") or "") == phase])
        and all(state == "done" for state in states)
    }
    gaps = {
        later: {
            earlier: sorted(files[later] & files[earlier])
            for earlier in phase_order[:index]
            if earlier not in ancestors.get(later, set()) and earlier not in done and files[later] & files[earlier]
        }
        for index, later in enumerate(phase_order)
    }
    return {later: gap for later, gap in gaps.items() if gap}


def parent_lines(parents: Mapping[str, set[str]], unreached: Mapping[str, Mapping[str, Sequence[str]]]) -> list[str]:
    """One line per phase, sorted by id: its parents, then any earlier phases it does not inherit."""

    def line(phase: str) -> str:
        base = f"  phase {phase}: parents {', '.join(sorted(parents.get(phase, ()))) or 'none'}"
        gap = unreached.get(phase, {})
        if not gap:
            return base
        missed = "; ".join(f"{earlier} ({', '.join(gap[earlier])})" for earlier in sorted(gap))
        return f"{base} | not inherited: {missed}"

    return [line(phase) for phase in sorted(set(parents) | set(unreached))]
