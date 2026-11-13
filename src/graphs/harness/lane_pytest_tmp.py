"""The rule that points a lane's pytest temp directories at a scratch directory under its run.

Pure: the environment and the paths are parameters, so nothing here reads the machine or the disk.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from pathlib import Path

ADDOPTS_VAR = "PYTEST_ADDOPTS"
TMPDIR_VAR = "TMPDIR"
BASETEMP_FLAG = "--basetemp"


def lane_scratch_dir(run_dir: Path, phase: str, task: str) -> Path:
    return run_dir / "pytest-tmp" / f"{phase}--{task}"


def _has_basetemp(addopts: str) -> bool:
    return any(t == BASETEMP_FLAG or t.startswith(BASETEMP_FLAG + "=") for t in shlex.split(addopts))


def basetemp_env(environ: Mapping[str, str], scratch: Path) -> dict[str, str]:
    """Existing PYTEST_ADDOPTS stay first; an operator's --basetemp wins, so it yields no ADDOPTS override."""
    existing = environ.get(ADDOPTS_VAR, "").strip()
    addopts = (
        {}
        if _has_basetemp(existing)
        else {ADDOPTS_VAR: " ".join(filter(None, [existing, f"{BASETEMP_FLAG}={scratch}"]))}
    )
    return {**addopts, TMPDIR_VAR: str(scratch)}
