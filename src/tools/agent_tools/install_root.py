"""Where a host's coxswain checkout is, and which python runs the harness: one rule every launch uses.

A migrated host has ONE checkout (a `pyproject.toml` named `coxswain`) holding the harness at
`<checkout>/src/graphs` and ONE venv at `<checkout>/.venv`. An unmigrated host still has the harness
in its own checkout with its own `<harness_dir>/.venv`, and that venv wins while it exists.
The deciders take the facts as arguments; `checkout_root` and `harness_python` read them from disk.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path

__all__ = [
    "DISTRIBUTION",
    "checkout_root",
    "choose_python",
    "harness_python",
    "legacy_python",
    "nearest_checkout",
    "project_name",
    "venv_python",
]

DISTRIBUTION = "coxswain"


def venv_python(root: Path) -> Path:
    return root / ".venv" / "bin" / "python"


def legacy_python(harness_dir: Path) -> Path:
    """The interpreter of an unmigrated host, whose harness checkout carries its own venv."""
    return venv_python(harness_dir)


def project_name(pyproject_text: str) -> str | None:
    """`[project] name` of a pyproject.toml's text; None when it is absent or the text does not parse."""
    try:
        name = tomllib.loads(pyproject_text).get("project", {}).get("name")
    except (tomllib.TOMLDecodeError, AttributeError):
        return None
    return name if isinstance(name, str) else None


def nearest_checkout(harness_dir: Path, names: Mapping[Path, str | None]) -> Path | None:
    """The nearest of `harness_dir` and its ancestors whose pyproject is named `coxswain`.

    `names` maps a directory to its pyproject's project name; a directory missing from it has no pyproject."""
    return next((d for d in (harness_dir, *harness_dir.parents) if names.get(d) == DISTRIBUTION), None)


def choose_python(harness_dir: Path, *, legacy_exists: bool, checkout: Path | None) -> Path:
    """The harness venv while it exists (an unmigrated host), else the checkout's venv.

    With neither, the legacy path is returned so a caller's existence check reports it missing."""
    if legacy_exists or checkout is None:
        return legacy_python(harness_dir)
    return venv_python(checkout)


def _read_name(directory: Path) -> str | None:
    try:
        return project_name((directory / "pyproject.toml").read_text(encoding="utf-8"))
    except OSError:
        return None


def checkout_root(harness_dir: str | Path) -> Path | None:
    """Edge. The coxswain checkout holding `harness_dir`, None on an unmigrated host."""
    start = Path(os.path.abspath(Path(harness_dir).expanduser()))
    names = {d: _read_name(d) for d in (start, *start.parents)}
    return nearest_checkout(start, names)


def harness_python(harness_dir: str | Path) -> Path:
    """Edge. The python that runs the harness at `harness_dir`; the caller checks it exists."""
    start = Path(harness_dir).expanduser()
    legacy = legacy_python(start)
    exists = legacy.is_file()
    return choose_python(start, legacy_exists=exists, checkout=None if exists else checkout_root(start))
