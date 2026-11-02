"""Which python a local lane runs, and the PATH it runs under. Pure: no I/O, plain data in and out.

The caller passes the running interpreter, the install checkout root the install step recorded, its PATH
and the set of paths that exist. Not yet wired into the launch.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path

from agent_tools import install_root as ir

__all__ = ["LanePython", "LaneRefusal", "candidates", "lane_path", "refuse_message", "resolve_lane_python"]


@dataclass(frozen=True)
class LanePython:
    bin_dir: Path
    path: str


@dataclass(frozen=True)
class LaneRefusal:
    message: str


def candidates(running: Path, install_root: Path | None) -> list[Path]:
    """The running interpreter, then the recorded install's venv python; in the order they are tried."""
    return [running] if install_root is None else [running, ir.venv_python(install_root)]


def lane_path(bin_dir: Path, caller_path: str) -> str:
    """`bin_dir` first, then the caller's entries; `bin_dir` and empty entries are not repeated."""
    rest = (e for e in caller_path.split(os.pathsep) if e and e != str(bin_dir))
    return os.pathsep.join([str(bin_dir), *rest])


def refuse_message(host: str, tried: Sequence[Path]) -> str:
    return f"{host}: python not on the lane PATH (tried {', '.join(str(p) for p in tried)})"


def resolve_lane_python(
    running: Path,
    install_root: Path | None,
    caller_path: str,
    existing: AbstractSet[Path],
    *,
    host: str,
) -> LanePython | LaneRefusal:
    """The first candidate in `existing` supplies the bin directory; with none, a refusal naming every candidate."""
    tried = candidates(running, install_root)
    chosen = next((c for c in tried if c in existing), None)
    if chosen is None:
        return LaneRefusal(refuse_message(host, tried))
    return LanePython(chosen.parent, lane_path(chosen.parent, caller_path))
