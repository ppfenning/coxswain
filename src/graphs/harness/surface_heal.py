from __future__ import annotations

from collections.abc import Mapping, Sequence

__all__ = ["heal_bare_filenames", "heal_new_markers", "malformed_surfaces", "notes_out_of_surfaces"]

_NEW = " (new)"
_GLOB_CHARS = frozenset("*?[]")


def _bare(surface: str) -> str:
    """The path with any trailing `(new)` marker and surrounding space removed."""
    return surface.strip().removesuffix("(new)").strip()


def _is_malformed(bare: str) -> bool:
    return bare.startswith("/") or ".." in bare.split("/") or any(c in _GLOB_CHARS for c in bare)


def malformed_surfaces(surfaces: Sequence[str]) -> list[str]:
    """Entries that are absolute, reach outside the repository with a `..` segment, or are a glob; input order."""
    return [s for s in surfaces if _is_malformed(_bare(s))]


def _missing(paths: frozenset[str], surface: str) -> bool:
    """A well-formed, unmarked surface that is neither a file in `paths` nor a directory with a file under it."""
    bare = _bare(surface)
    return (
        not surface.strip().endswith("(new)")
        and bool(bare)
        and not _is_malformed(bare)
        and bare not in paths
        and not any(p.startswith(f"{bare.rstrip('/')}/") for p in paths)
    )


def heal_new_markers(task: Mapping[str, object], paths: frozenset[str]) -> dict[str, object]:
    """A new task whose missing surfaces carry `(new)`, with one lint note each after any existing lint."""
    surfaces = [str(s) for s in task.get("surfaces") or []]
    healed = [_missing(paths, s) for s in surfaces]
    if not any(healed):
        return dict(task)
    notes = [
        f"{_bare(s)}: missing from the repository, healed by the surface check"
        for s, is_healed in zip(surfaces, healed, strict=True)
        if is_healed
    ]
    marked = [f"{s}{_NEW}" if is_healed else s for s, is_healed in zip(surfaces, healed, strict=True)]
    return {**task, "surfaces": marked, "lint": [*(task.get("lint") or []), *notes]}


def _is_bare_filename(bare: str) -> bool:
    """A note such as `two words` or `key: text` is not a filename, so it is left for `notes_out_of_surfaces`."""
    return bool(bare) and "/" not in bare and not _is_malformed(bare) and not _is_note(bare)


def _body_paths(body: str, name: str) -> frozenset[str]:
    """Slash-bearing, well-formed words in `body` that end in `/name`; a leading `.` is part of the path."""
    words = (w.lstrip("`'\"([<").rstrip("`'\")]>,;:.") for w in body.split())
    return frozenset(w for w in words if w.endswith(f"/{name}") and not _is_malformed(w))


def _resolve_bare(name: str, paths: frozenset[str], body: str) -> str | None:
    in_tree = [p for p in paths if p.rsplit("/", 1)[-1] == name]
    if len(in_tree) == 1:
        return in_tree[0]
    named = _body_paths(body, name)
    if len(named) == 1:
        (path,) = named
        return path if path in paths else f"{path}{_NEW}"
    return None


def heal_bare_filenames(task: Mapping[str, object], paths: frozenset[str]) -> dict[str, object]:
    """A new task whose bare-filename surfaces are full repository paths, or left out with a lint note each."""
    surfaces = [str(s) for s in task.get("surfaces") or []]
    bares = [_bare(s) for s in surfaces]
    is_bare = [_is_bare_filename(b) for b in bares]
    if not any(is_bare):
        return dict(task)
    body = task.get("body")
    text = body if isinstance(body, str) else ""
    resolved = [
        _resolve_bare(b, paths, text) if flag else s for s, b, flag in zip(surfaces, bares, is_bare, strict=True)
    ]
    notes = list(
        dict.fromkeys(
            f"{b}: no unique repository path, left out of surfaces"
            for b, r, flag in zip(bares, resolved, is_bare, strict=True)
            if flag and r is None
        )
    )
    healed = {**task, "surfaces": list(dict.fromkeys(r for r in resolved if r is not None))}
    return {**healed, "lint": [*(task.get("lint") or []), *notes]} if notes else healed


def _is_note(surface: str) -> bool:
    bare = _bare(surface)
    return any(c.isspace() for c in bare) or ": " in bare


def notes_out_of_surfaces(task: Mapping[str, object]) -> dict[str, object]:
    """A new task whose note-like surfaces (whitespace, or a colon and a space) moved to lint after any existing lint."""
    surfaces = [str(s) for s in task.get("surfaces") or []]
    if not any(_is_note(s) for s in surfaces):
        return dict(task)
    lint = [*(task.get("lint") or [])]
    moved = [s for s in dict.fromkeys(s for s in surfaces if _is_note(s)) if s not in lint]
    return {**task, "surfaces": [s for s in surfaces if not _is_note(s)], "lint": [*lint, *moved]}
