"""Pure selection of integration checks for the packages a change touches.

A check is a mapping with `name` and `cmd`, the same entry `check_scope` and `checks` use; other keys pass through.
The caller supplies the changed paths; nothing is read here.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from harness.check_scope import Check, ScopedChecks

PLACEHOLDER = "{packages}"
SOURCE_TREES = frozenset({"src"})


def package_of(path: str) -> str:
    """Two segments (src/graphs) when a path of three or more segments is under src/, else the first segment."""
    parts = path.split("/")
    return "/".join(parts[:2]) if len(parts) >= 3 and parts[0] in SOURCE_TREES else parts[0]


def _fill(check: Check, packages: str) -> dict[str, Any]:
    return {**check, "cmd": check.get("cmd", "").replace(PLACEHOLDER, packages)}


def scope_integration(checks: Sequence[Check], changed_paths: Sequence[str]) -> ScopedChecks:
    """Checks to run with `{packages}` filled; none when no path changed. Selection keys: packages, reason, ran."""
    packages = sorted({package_of(p) for p in changed_paths})
    run = tuple(_fill(c, " ".join(packages)) for c in checks) if packages else ()
    reason = "packages touched" if packages else "nothing touched"
    return ScopedChecks(run, {"packages": packages, "reason": reason, "ran": [c.get("name") for c in run]})
