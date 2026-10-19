"""Release versions and tags, pure: CalVer `YYYY.M.N` from 2026.10.0 on, the old SemVer tags below it.

A release is `YYYY.M.N` (N counts from 0 within a month, the month unpadded) and its tag is `vYYYY.M.N`.
Hosts upgrade from the multi-repo `0.x` line (the last is `0.47.0`), so a SemVer tag must still parse,
and must sort below every CalVer tag whatever its numbers say. A legacy `-beta.N` suffix parses too
(it sorts below its own release); CalVer has no pre-release form, status lives in the manifest.

Every function here takes strings and returns plain data: no file, process or network access."""

from __future__ import annotations

import re
from collections.abc import Iterable

CALVER_RE = re.compile(r"^(\d{4})\.(1[0-2]|[1-9])\.(0|[1-9]\d*)$")
_LEGACY_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-beta\.(\d+))?$")
# A version is CalVer when its first number is a year; no SemVer major here ever reached 1000.
_YEAR_FLOOR = 1000


def bare(version: str) -> str:
    """`version` without surrounding whitespace or one leading `v`."""
    return version.strip().removeprefix("v")


def release_tag(version: str) -> str:
    """The tag a release carries: a leading `v`, whatever `version` was given with."""
    return "v" + bare(version)


def is_calver(version: str) -> bool:
    """True for `YYYY.M.N` (or `vYYYY.M.N`): a four-digit year, an unpadded month 1-12, N from 0."""
    return CALVER_RE.match(bare(version)) is not None


def release_key(version: str) -> tuple | None:
    """The sort key of a release version or tag, or None when it is neither CalVer nor legacy SemVer.

    `(1, year, month, n)` for CalVer and `(0, major, minor, patch, final, beta)` for the legacy line, so every
    legacy tag sorts below every CalVer tag; a legacy `-beta.N` sorts below its own release."""
    text = bare(version)
    cal = CALVER_RE.match(text)
    if cal:
        return (1, *map(int, cal.groups()))
    old = _LEGACY_RE.match(text)
    if old is None or int(old.group(1)) >= _YEAR_FLOOR:
        return None
    major, minor, patch, beta = old.groups()
    return (0, int(major), int(minor), int(patch), 0 if beta is not None else 1, int(beta or 0))


def is_release(version: str) -> bool:
    """True when `version` (or tag) parses as either a CalVer or a legacy SemVer release."""
    return release_key(version) is not None


def newest(tags: Iterable[str], *, finals_only: bool = True) -> str | None:
    """The greatest release-shaped tag in `tags`, in its own spelling; order-independent.
    `finals_only` skips a legacy `-beta.N`, which is never a default upgrade target."""
    keyed = [(key, tag) for tag in tags if (key := release_key(tag)) is not None
             and not (finals_only and "-" in tag)]
    return max(keyed)[1] if keyed else None


def previous(tags: Iterable[str], version: str) -> str | None:
    """The greatest release-shaped tag strictly below `version`, in its own spelling, or None."""
    ceiling = release_key(version)
    if ceiling is None:
        return None
    below = [(key, tag) for tag in tags if (key := release_key(tag)) is not None and key < ceiling]
    return max(below)[1] if below else None


def pep440(version: str) -> str:
    """The version PyPI files `version` under: CalVer as written, a legacy `-beta.N` as `bN`."""
    return re.sub(r"-beta\.(\d+)$", r"b\1", bare(version))
