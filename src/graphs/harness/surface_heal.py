from __future__ import annotations

from collections.abc import Mapping, Sequence

__all__ = ["heal_new_markers", "malformed_surfaces"]

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
