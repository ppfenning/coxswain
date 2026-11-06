from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from harness.surface_heal import _bare, _is_malformed

__all__ = ["NameFindings", "check_names"]

_BODY_KEY = "body"  # unknown: the epic wiring task confirms the key that holds the ticket text
_SENTENCES = re.compile(r"(?<=[.!?])\s+|[\n;]+")
_PATH = re.compile(r"(?<![\w./:@-])[\w.-]+(?:/[\w.-]+)*\.[A-Za-z]\w*(?![\w/-])")
_CHANGES = re.compile(
    r"\b(?:edit(?:s|ed|ing)?|chang(?:e|es|ed|ing)|modif(?:y|ies|ied|ying)|updat(?:e|es|ed|ing)"
    r"|rewrit(?:e|es|ing)|rewrote|delet(?:e|es|ed|ing)|remov(?:e|es|ed|ing)|retir(?:e|es|ed|ing))\b",
    re.IGNORECASE,
)
_CREATES = re.compile(
    r"\b(?:add(?:s|ed|ing)?|new|creat(?:e|es|ed|ing)|introduc(?:e|es|ed|ing)|defin(?:e|es|ed|ing))\b",
    re.IGNORECASE,
)
_NAME = r"([A-Za-z_][\w.]*)"
_ARGS = r"\([^)`]*\)"
_IDENTIFIERS = (
    re.compile(rf"`{_NAME}{_ARGS}`"),
    re.compile(rf"\b(?:function|class|method)\s+`{_NAME}(?:{_ARGS})?`", re.IGNORECASE),
    re.compile(rf"`{_NAME}(?:{_ARGS})?`\s+(?:function|class|method)\b", re.IGNORECASE),
)


@dataclass(frozen=True)
class NameFindings:
    task: dict[str, object]
    corrections: tuple[str, ...]


def _sentences(body: str) -> list[str]:
    return [s for s in _SENTENCES.split(body) if s.strip()]


def _resolve_bare(surface: str, paths: frozenset[str]) -> tuple[str, str | None, str | None]:
    """The surface to keep, a lint note when it was resolved, a correction when it could not be."""
    bare = _bare(surface)
    if (
        not bare
        or surface.strip().endswith("(new)")
        or "/" in bare
        or _is_malformed(bare)
        or bare in paths
        or any(p.startswith(f"{bare}/") for p in paths)
    ):
        return surface, None, None
    found = sorted(p for p in paths if p.endswith(f"/{bare}"))
    if len(found) == 1:
        return found[0], f"{bare}: resolved to {found[0]}, healed by the ticket check", None
    if found:
        return (
            surface,
            None,
            f"{bare} matches several files: {', '.join(found)}. The surface stays bare until one is named.",
        )
    return surface, None, f"{bare} was not found in the repository. No file path ends in that name."


def _outside_repo(surfaces: Sequence[str]) -> list[str]:
    return [
        f"{bare} lies outside the repository. It is reported here and not healed."
        for bare in map(_bare, surfaces)
        if bare.startswith("/") or ".." in bare.split("/")
    ]


def _covered(path: str, surfaces: Sequence[str]) -> bool:
    return any(path == bare or path.startswith(f"{bare.rstrip('/')}/") for bare in map(_bare, surfaces) if bare)


def _unmarked_missing(body: str, paths: frozenset[str], surfaces: Sequence[str]) -> list[str]:
    """Slash-bearing file names in the body that are not in `paths` and not covered by a surface; first-seen order."""
    named = dict.fromkeys(m.group() for m in _PATH.finditer(body))
    return [
        f"{p} is not in the repository and the surface list does not mark it (new). Mark it (new) if the task creates it."
        for p in named
        if "/" in p and ".." not in p.split("/") and p not in paths and not _covered(p, surfaces)
    ]


def _changing_files(body: str, paths: frozenset[str], surfaces: Sequence[str]) -> list[str]:
    """Files in `paths` named after a change verb in their sentence and covered by no surface; first-seen order."""
    named = dict.fromkeys(
        m.group()
        for s in _sentences(body)
        for verb in [_CHANGES.search(s)]
        if verb
        for m in _PATH.finditer(s, verb.end())
        if m.group() in paths
    )
    return [p for p in named if not _covered(p, surfaces)]


def _unfound_names(body: str, defined: Callable[[str], Sequence[str]]) -> list[str]:
    """Identifiers written as calls or next to function, class or method that `defined` cannot place."""
    names = dict.fromkeys(
        m.group(1).rsplit(".", 1)[-1]
        for s in _sentences(body)
        if not _CREATES.search(s)
        for pattern in _IDENTIFIERS
        for m in pattern.finditer(s)
    )
    return [
        f"{name} was not found in the tree. No def or class of that name exists." for name in names if not defined(name)
    ]


def check_names(
    task: Mapping[str, object],
    paths: frozenset[str],
    defined: Callable[[str], Sequence[str]],
) -> NameFindings:
    """The ticket with bare surfaces resolved and body-named changing files added, plus corrections it could not heal."""
    raw = task.get("surfaces")
    surfaces = [str(s) for s in raw] if isinstance(raw, (list, tuple)) else []
    text = task.get(_BODY_KEY)
    body = text if isinstance(text, str) else ""
    resolved = [_resolve_bare(s, paths) for s in surfaces]
    kept = [r[0] for r in resolved]
    added = _changing_files(body, paths, kept)
    notes = [r[1] for r in resolved if r[1]] + [
        f"added to surfaces: {p} was named in the body and healed by the ticket check" for p in added
    ]
    corrections = (
        *(r[2] for r in resolved if r[2]),
        *_outside_repo(kept),
        *_unmarked_missing(body, paths, kept),
        *_unfound_names(body, defined),
    )
    if not notes:
        return NameFindings(task=dict(task), corrections=corrections)
    lint = task.get("lint")
    healed = {
        **task,
        "surfaces": [*kept, *added],
        "lint": [*(lint if isinstance(lint, (list, tuple)) else []), *notes],
    }
    return NameFindings(task=healed, corrections=corrections)
