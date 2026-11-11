"""Pure path-layout classifier: which known repository a path belongs to, from its shape alone."""

from __future__ import annotations

import re
from typing import NamedTuple

from graphs.delivery.decompose_size import NEW_MARKER

__all__ = ["LAYOUT", "foreign_repo", "repo_key", "repo_of", "repo_path"]


class Row(NamedTuple):
    key: str
    prefixes: tuple[str, ...]
    files: tuple[str, ...]
    src_ext: str | None  # a file with this suffix directly under `src/` belongs to the row


# One row per known repository. A third repository is one more row.
LAYOUT: tuple[Row, ...] = (
    Row("coxswain", ("src/tools/", "src/graphs/", "src/cartridges/", "devtools/", "crew/"), (), None),
    Row("coxswain-dash", ("themes/", "src/ui/"), ("Cargo.toml", "Cargo.lock"), ".rs"),
)

_NOTATION = re.compile(r"^[<$\{]|\s")


def _row_match(row: Row, path: str) -> bool:
    direct_src = path.startswith("src/") and "/" not in path[len("src/") :]
    return (
        path in row.files
        or path.startswith(row.prefixes)
        or (row.src_ext is not None and direct_src and path.endswith(row.src_ext))
    )


def repo_of(path: str) -> str | None:
    """Key of the repository `path` belongs to; None for unknown, rooted, tilde or notation paths."""
    bare = path.removesuffix(NEW_MARKER)
    if not bare or bare.startswith(("/", "~")) or _NOTATION.search(bare):
        return None
    return next((row.key for row in LAYOUT if _row_match(row, bare)), None)


def repo_key(repo: str) -> str | None:
    """Key named by the last segment of an initiative `repo` value; None if it is not a known repository."""
    last = repo.rstrip("/").rsplit("/", 1)[-1]
    return next((row.key for row in LAYOUT if row.key == last), None)


def repo_path(key: str) -> str:
    """Tilde-form `repo` value for a known key. Raises KeyError for an unknown key."""
    if all(row.key != key for row in LAYOUT):
        raise KeyError(key)
    return f"~/repos/{key}"


def foreign_repo(path: str, own_key: str) -> str | None:
    """The known key `path` belongs to when it differs from `own_key`, else None."""
    found = repo_of(path)
    return found if found is not None and found != own_key else None
