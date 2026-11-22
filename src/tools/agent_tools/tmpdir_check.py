"""The doctor's `lane TMPDIR` rows: whether the temp directory each installed unit gives its lanes is fit for a lane's
scratch, a smoke's installs and a full suite. Pure over mount facts; the edge (`cli_doctor`) reads the unit files and
`/proc/self/mountinfo`.

A tmpfs is RAM, so a suite there competes with the machine's memory, and a tmpfs mounted with `usrquota` refused
writes partway through one (E1, 2026-11-21: 85 `OSError: [Errno 122] Disk quota exceeded`). A disk with a per-user
quota under about 20 GB is the same failure, later. Each finding is a warning, never a failure: the lanes still run.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Sequence
from dataclasses import dataclass

CHECK = "lane TMPDIR"
QUOTA_FLOOR_BYTES = 20 * 1024**3  # about 20 GB: room for a full suite and a smoke's installs side by side
_QUOTA_OPTIONS = frozenset({"usrquota", "quota", "uquota"})
_HARDLIMIT = "usrquota_block_hardlimit="
_SIZE = re.compile(r"(\d+)([kmgtp]?)", re.IGNORECASE)
_ESCAPE = re.compile(r"\\([0-7]{3})")


@dataclass(frozen=True)
class Mount:
    point: str
    fstype: str
    options: frozenset[str]  # the mount's own options and the filesystem's super options, together


@dataclass(frozen=True)
class UnitTmpdir:
    """One installed unit's TMPDIR: `tmpdir` as it reads, `resolved` with symlinks followed (the edge's realpath), and
    `source` the file that set it: "unit", a drop-in's name, or None when nothing sets it (Python then uses /tmp)."""

    unit: str
    tmpdir: str
    resolved: str
    source: str | None


def _unescape(field: str) -> str:
    return _ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), field)


def parse_mountinfo(text: str) -> list[Mount]:
    """The mounts of a `/proc/self/mountinfo` text, in its order; a line that does not parse is skipped."""
    mounts = []
    for line in text.splitlines():
        before, sep, after = line.partition(" - ")
        fields, tail = before.split(), after.split()
        if not sep or len(fields) < 6 or len(tail) < 3:
            continue
        options = frozenset(fields[5].split(",")) | frozenset(tail[2].split(","))
        mounts.append(Mount(_unescape(fields[4]), tail[0], options))
    return mounts


def _under(path: str, point: str) -> bool:
    return point == "/" or path == point or path.startswith(point.rstrip("/") + "/")


def mount_of(path: str, mounts: Sequence[Mount]) -> Mount | None:
    """The mount `path` (absolute, symlinks resolved) is on: the deepest mount point above it, the later of two on the
    same point (the one mounted over the other)."""
    above = [(len(m.point), i, m) for i, m in enumerate(mounts) if _under(posixpath.normpath(path), m.point)]
    return max(above, key=lambda item: (item[0], item[1]))[2] if above else None


def _size(text: str) -> int | None:
    found = _SIZE.fullmatch(text.strip())
    if found is None:
        return None
    return int(found.group(1)) * 1024 ** " kmgtp".index((found.group(2) or " ").lower())


def quota_limit(options: frozenset[str]) -> int | None:
    """A tmpfs's default per-user block limit, in bytes, from `usrquota_block_hardlimit=`; None when none is given."""
    values = [_size(option[len(_HARDLIMIT):]) for option in options if option.startswith(_HARDLIMIT)]
    return next((value for value in values if value is not None), None)


def _has_quota(options: frozenset[str]) -> bool:
    return bool(options & _QUOTA_OPTIONS) or any(o.startswith(("usrjquota=", _HARDLIMIT)) for o in options)


def _gb(size: int) -> str:
    return f"{size / 1024**3:.1f} GB"


def finding(tmpdir: str, mount: Mount) -> str | None:
    """Pure. Why `tmpdir`, on `mount`, is unfit for a lane's temp files, or None when it is fit."""
    limit = quota_limit(mount.options)
    on = f"{tmpdir} is on {mount.point} ({mount.fstype}"
    if mount.fstype == "tmpfs":
        quota = f", user quota {_gb(limit)}" if limit is not None else ", with user quotas" if _has_quota(mount.options) else ""
        return f"{on}{quota}), which is RAM: a suite or a smoke's installs there can run out of room"
    if limit is not None and limit < QUOTA_FLOOR_BYTES:
        return f"{on}) with a user quota of {_gb(limit)}, under {_gb(QUOTA_FLOOR_BYTES)}"
    if limit is None and _has_quota(mount.options):
        return f"{on}) with user quotas on; check the limit is at least {_gb(QUOTA_FLOOR_BYTES)} with `quota -s`"
    return None


def _fix(seen: UnitTmpdir, default: str) -> str:
    if seen.source is None:
        return f"run `cox upgrade`, which sets TMPDIR={default} in the unit"
    if seen.source == "unit":
        return "set lane_tmpdir in the profile to a directory on disk, then run `cox upgrade`"
    return f"change or remove the drop-in {seen.source}, which overrides the unit's TMPDIR"


def rows(units: Sequence[UnitTmpdir], mounts: Sequence[Mount] | None, default: str) -> list[dict]:
    """Pure. One row per installed unit, a warning naming the unit and its fix when its TMPDIR is unfit (`finding`);
    one passing row when no unit is installed or no mount facts were read (a platform without mountinfo). `default` is
    the TMPDIR `cox upgrade` would set."""
    if not units:
        return [{"check": CHECK, "ok": True, "detail": "no unit installed"}]
    if mounts is None:
        return [{"check": CHECK, "ok": True, "detail": "not checked: no mount table to read"}]
    out = []
    for seen in units:
        mount = mount_of(seen.resolved, mounts)
        why = finding(seen.tmpdir, mount) if mount is not None else None
        origin = "no TMPDIR set, so /tmp" if seen.source is None else f"TMPDIR from {seen.source}"
        if why is None:
            out.append({"check": CHECK, "ok": True, "detail": f"{seen.unit}: {seen.tmpdir} ({origin})"})
        else:
            out.append({"check": CHECK, "ok": True, "warn": True,
                        "detail": f"{seen.unit}: {why} ({origin}); fix: {_fix(seen, default)}"})
    return out
