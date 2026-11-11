"""Pure sort of planned tasks into this repository, another repository, or neither."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any, NamedTuple

from graphs.delivery.decompose_size import NEW_MARKER
from graphs.delivery.repo_layout import foreign_repo, repo_path

Task = dict[str, Any]


class Split(NamedTuple):
    kept: list[Task]
    follow_ups: list[dict[str, Any]]
    dropped: list[dict[str, Any]]
    notes: list[str]


class _Sorted(NamedTuple):
    own: list[str]
    foreign: dict[str, list[str]]
    neither: list[str]


class _Placed(NamedTuple):
    kept: Task | None
    dropped: dict[str, Any] | None
    moved: list[tuple[str, Task]]
    notes: list[str]


def _has(paths: Collection[str], path: str, is_new: bool) -> bool:
    """A new file is judged by its directory: does any tracked path sit under it."""
    if not is_new:
        return path in paths
    prefix = path.rpartition("/")[0]
    prefix = prefix + "/" if prefix else ""
    return any(p.startswith(prefix) for p in paths)


def classify(
    surface: str,
    own_paths: Collection[str],
    foreign_trees: Mapping[str, Collection[str]],
    own_key: str | None = None,
) -> tuple[str, str | None]:
    """Return ('own', None), ('foreign', repo) or ('neither', None). Own wins over foreign.

    A surface no listing places falls back to its path layout when `own_key` is known."""
    is_new = surface.endswith(NEW_MARKER)
    path = surface.removesuffix(NEW_MARKER)
    if _has(own_paths, path, is_new):
        return ("own", None)
    homes = [repo for repo, paths in foreign_trees.items() if _has(paths, path, is_new)]
    if homes:
        return ("foreign", homes[0]) if len(homes) == 1 else ("neither", None)
    key = foreign_repo(path, own_key) if own_key is not None else None
    return ("foreign", repo_path(key)) if key is not None else ("neither", None)


def _sort_surfaces(
    surfaces: list[str],
    own_paths: Collection[str],
    foreign_trees: Mapping[str, Collection[str]],
    own_key: str | None,
) -> _Sorted:
    kinds = [(s, *classify(s, own_paths, foreign_trees, own_key)) for s in surfaces]
    repos = list(dict.fromkeys(r for _, k, r in kinds if k == "foreign" and r is not None))
    return _Sorted(
        [s for s, k, _ in kinds if k == "own"],
        {repo: [s for s, k, r in kinds if k == "foreign" and r == repo] for repo in repos},
        [s for s, k, _ in kinds if k == "neither"],
    )


def _labels(repos: list[str]) -> dict[str, str]:
    """Last path segment per repo. Widen with parent segments only where receiving repos clash."""
    parts = {r: [p for p in r.split("/") if p] or [r] for r in repos}

    def widen(depth: dict[str, int]) -> dict[str, str]:
        labels = {r: "-".join(parts[r][-depth[r] :]) for r in repos}
        clash = {r for r in repos if list(labels.values()).count(labels[r]) > 1 and depth[r] < len(parts[r])}
        return labels if not clash else widen({r: depth[r] + (r in clash) for r in repos})

    return widen(dict.fromkeys(repos, 1))


def _place(task: Task, s: _Sorted, labels: Mapping[str, str]) -> _Placed:
    tid, surfaces = task["id"], task.get("surfaces") or []
    if not s.foreign and not s.neither:
        return _Placed(task, None, [], [])
    if not s.own and not s.foreign:
        reason = f"None of its surfaces is in this repository or a registered one: {', '.join(surfaces)}."
        return _Placed(None, {"task": task, "reason": reason}, [], [f"Dropped task {tid}. {reason}"])
    whole = not s.own and len(s.foreign) == 1
    moved = [
        (repo, {**task, "surfaces": surf, "id": tid if whole else f"{tid}--{labels[repo]}"})
        for repo, surf in s.foreign.items()
    ]
    notes = [f"Removed surface {x} from task {tid}: it is in no known repository." for x in s.neither] + [
        (
            f"Task {tid} keeps its own surfaces; moved {', '.join(copy['surfaces'])} "
            f"to {copy['id']} in the follow-up for {labels[repo]}."
            if s.own
            else f"Task {tid} has no surface here; moved {', '.join(copy['surfaces'])} "
            f"as {copy['id']} to the follow-up for {labels[repo]}."
        )
        for repo, copy in moved
    ]
    return _Placed({**task, "surfaces": s.own} if s.own else None, None, moved, notes)


def _drop_edges(task: Task, fates: Mapping[str, str]) -> tuple[Task, list[str]]:
    needs = task.get("needs") or []
    gone = [n for n in needs if n in fates]
    if not gone:
        return task, []
    notes = [f"Removed edge: task {task['id']} no longer needs {n}, which {fates[n]}." for n in gone]
    return {**task, "needs": [n for n in needs if n not in fates]}, notes


def _describe(task: Task, ids: list[str]) -> str:
    needs = [n for n in task.get("needs") or [] if n in ids]
    return "\n".join(
        [
            f"{task['id']}: {task.get('title', '')}",
            f"Surfaces: {', '.join(task.get('surfaces') or [])}",
            f"Needs: {', '.join(needs) or 'none'}",
            "",
            task.get("body", ""),
        ]
    )


def _follow_up(repo: str, moved: list[Task], title: str, label: str) -> dict[str, Any]:
    ids = [t["id"] for t in moved]
    return {
        "repo": repo,
        "title": f"{title}: follow-up for {label}",
        "body": "\n\n".join(_describe(t, ids) for t in moved),
        "tasks": ids,
        "lint": (
            f"Split out of the initiative {title}: {', '.join(ids)} touch files in {repo}, not in this repository."
        ),
    }


def split_foreign(
    tasks: list[Task],
    own_paths: Collection[str],
    foreign_trees: Mapping[str, Collection[str]],
    *,
    title: str,
    own_key: str | None = None,
) -> Split:
    """Sort each task's surfaces into this repository, one other repository, or neither."""
    if not own_paths:
        return Split(list(tasks), [], [], [])
    own = frozenset(own_paths)
    trees = {repo: frozenset(paths) for repo, paths in foreign_trees.items()}
    sorted_ = [_sort_surfaces(t.get("surfaces") or [], own, trees, own_key) for t in tasks]
    labels = _labels(list(dict.fromkeys(r for s in sorted_ for r in s.foreign)))
    placed = [_place(t, s, labels) for t, s in zip(tasks, sorted_)]
    fates = {
        t["id"]: "was dropped"
        if p.dropped
        else "was moved to the follow-up for " + " and ".join(labels[r] for r, _ in p.moved)
        for t, p in zip(tasks, placed)
        if p.kept is None
    }
    kept_pairs = [_drop_edges(p.kept, fates) for p in placed if p.kept is not None]
    moves = [m for p in placed for m in p.moved]
    return Split(
        [k for k, _ in kept_pairs],
        [_follow_up(repo, [t for r, t in moves if r == repo], title, label) for repo, label in labels.items()],
        [p.dropped for p in placed if p.dropped is not None],
        [n for p in placed for n in p.notes] + [n for _, ns in kept_pairs for n in ns],
    )
