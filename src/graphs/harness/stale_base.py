"""Pure decision helpers for an empty build that came from a stale base. No I/O, git, clock or store."""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Sequence

from graphs.delivery.repo_layout import foreign_repo

_NEW_MARKER = "(new)"

# Not preceded by a path character, so the tail of an absolute path or a URL never starts a match.
_BODY_PATH = re.compile(r"(?<![\w./~:-])[\w.-]+(?:/[\w.-]*[\w-])+")
_EXTENSION = re.compile(r"\.[A-Za-z][A-Za-z0-9]*$")


def _is_relative(path: str) -> bool:
    return bool(path) and not path.startswith(("/", "~"))


def _body_paths(body: str) -> tuple[str, ...]:
    """Directory-and-extension paths in prose; one followed by `(new)` is a file the ticket creates, so it is skipped."""
    return tuple(
        m.group(0)
        for m in _BODY_PATH.finditer(body)
        if _EXTENSION.search(m.group(0)) and not body[m.end() :].lstrip(" ").startswith(_NEW_MARKER)
    )


def ticket_paths(surfaces: Sequence[str], body: str) -> tuple[str, ...]:
    """Repository files the ticket names, sorted. A surface marked `(new)` is not named; absolute paths and duplicates are dropped."""
    from_surfaces = tuple(s.strip().rstrip("/") for s in surfaces if not s.strip().endswith(_NEW_MARKER))
    return tuple(sorted({p for p in (*from_surfaces, *_body_paths(body)) if _is_relative(p)}))


def missing_paths(named: Iterable[str], tree: Collection[str]) -> tuple[str, ...]:
    """The named paths absent from `tree`, sorted. A directory with a tree path under it is present."""
    return tuple(
        sorted({p for p in named if p not in tree and not any(t.startswith(f"{p.rstrip('/')}/") for t in tree)})
    )


def is_patch_empty(failure: str) -> bool:
    """True for the runner's `patch_empty` failure; `patch_outside_scratch` is a different fault."""
    return "patch_empty" in failure and "patch_outside_scratch" not in failure


def stale_base_reason(task: str, missing: Iterable[str]) -> str:
    """The quarantine reason: every missing path, sorted, and why the build produced nothing."""
    return f"{task}: the build base predates files the ticket needs: {', '.join(sorted(set(missing)))}"


# Must equal `harness.cause_rule._WRONG_REPOSITORY_PREFIX`, which imports nothing from here.
WRONG_REPOSITORY_PREFIX = "wrong repository:"


def wrong_repository(missing: Iterable[str], own_key: str | None) -> str | None:
    """The one other known repository every missing path belongs to; None when the paths are mixed, unknown or own."""
    paths = tuple(missing)
    if own_key is None or not paths:
        return None
    homes = {foreign_repo(p, own_key) for p in paths}
    return next(iter(homes)) if len(homes) == 1 and None not in homes else None


def wrong_repository_reason(task: str, missing: Iterable[str], repo: str) -> str:
    """The quarantine reason for a ticket whose missing files all live in another repository: the ticket's fault."""
    return f"{WRONG_REPOSITORY_PREFIX} {task} names {', '.join(sorted(set(missing)))}, which belong to {repo}, not this repository"
