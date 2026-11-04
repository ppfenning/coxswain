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
