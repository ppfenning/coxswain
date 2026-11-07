"""Pure ordering of decomposed tasks that share a surface, within and across phases."""

from __future__ import annotations

from typing import Any

from graphs.delivery.decompose_size import NEW_MARKER


def _files(task: dict[str, Any]) -> list[str]:
    return [s.removesuffix(NEW_MARKER) for s in task.get("surfaces") or []]


def _reaches(needs: dict[str, list[str]], src: str, dst: str) -> bool:
    seen: set[str] = set()
    stack = [src]
    while stack:
        node = stack.pop()
        if node == dst:
            return True
        if node not in seen:
            seen.add(node)
            stack.extend(n for n in needs.get(node, []) if n in needs)
    return False


def order_shared_surfaces(tasks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Add a needs edge, later to earlier, for unordered same-phase tasks sharing a file."""
    out = [{**t, "needs": list(t["needs"])} if "needs" in t else dict(t) for t in tasks]
    needs = {t.get("id"): list(t.get("needs") or []) for t in tasks}
    phases = list(dict.fromkeys(t.get("phase") for t in tasks))
    notes: list[str] = []
    for phase in phases:
        members = [i for i, t in enumerate(tasks) if t.get("phase") == phase]
        for pos, later_i in enumerate(members):
            later = tasks[later_i]
            for earlier_i in members[:pos]:
                earlier = tasks[earlier_i]
                later_files = set(_files(later))
                shared = list(dict.fromkeys(f for f in _files(earlier) if f in later_files))
                a, b = earlier.get("id"), later.get("id")
                if not shared or _reaches(needs, a, b) or _reaches(needs, b, a):
                    continue
                if a not in needs[b]:
                    needs[b].append(a)
                    out[later_i]["needs"] = [*out[later_i].get("needs", []), a]
                notes.append(f"ordered: {b} now needs {a}, both touch {', '.join(shared)}")
    return out, notes


def order_cross_phase_surfaces(
    tasks: list[dict[str, Any]], phase_order: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Add a needs edge, later phase to earlier, for tasks in different phases sharing a file.

    Sees shared files only, not a table or column named in prose, so a schema change
    read through a different file still needs the launch-time view in the later phase.
    """
    out = [{**t, "needs": list(t["needs"])} if "needs" in t else dict(t) for t in tasks]
    needs = {t.get("id"): list(t.get("needs") or []) for t in tasks}
    rank = {phase: i for i, phase in enumerate(phase_order)}
    ranked = sorted((i for i, t in enumerate(tasks) if t.get("phase") in rank), key=lambda i: rank[tasks[i]["phase"]])
    notes: list[str] = []
    for pos, later_i in enumerate(ranked):
        later = tasks[later_i]
        later_files = set(_files(later))
        nearest_first = [i for i in reversed(ranked[:pos]) if rank[tasks[i]["phase"]] < rank[later["phase"]]]
        for earlier_i in nearest_first:
            earlier = tasks[earlier_i]
            shared = list(dict.fromkeys(f for f in _files(earlier) if f in later_files))
            a, b = earlier.get("id"), later.get("id")
            if not shared or _reaches(needs, b, a) or _reaches(needs, a, b):
                continue
            needs[b].append(a)
            out[later_i]["needs"] = [*out[later_i].get("needs", []), a]
            notes.append(f"ordered: {b} now needs {a}, both touch {', '.join(shared)}")
    return out, notes
