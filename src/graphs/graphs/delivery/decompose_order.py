"""Pure ordering of same-phase decomposed tasks that share a surface."""

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
