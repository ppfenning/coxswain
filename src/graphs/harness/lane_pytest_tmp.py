"""The rule that points a lane's temp files, pytest's among them, at a scratch directory of the lane's own.

The scratch directory sits under the system temp directory, which is where a hand run's pytest makes its temp paths.
Under the run's worktree directory (`~/worktrees` on a lane host) a suite's temp paths were under the home directory,
and a suite that reads a path under a home directory as a home path failed in every lane while passing by hand.

Its path is short and spells out neither the run nor the task, only digests of them. On macOS the system temp directory
is already about fifty characters, and a path that also spelled out the run id, the phase and the task put pytest's temp
paths past two hundred: a folder a test named for a whole temp path went past the 255-byte file name limit, an error cut
at two hundred characters lost its tail, and the root's `coxswain-` matched what a test asserted was absent.

Only TMPDIR moves. pytest finds its temp root through it, and a pytest the suite starts itself makes its own numbered
directory beside the suite's. A `--basetemp` exported in PYTEST_ADDOPTS reached that nested pytest too, which then
refused to run below it, or emptied it when it ran elsewhere. PYTEST_ADDOPTS is left as the caller has it.

Pure: the environment and the paths are parameters, so nothing here reads the machine or the disk.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

TMPDIR_VAR = "TMPDIR"
DIGEST_CHARS = 12


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:DIGEST_CHARS]


def run_scratch_root(temp_root: Path, run_dir: Path) -> Path:
    """The run's scratch directory under `temp_root`, named for a digest of `run_dir`'s whole path.

    Two runs with one id under different worktree roots never share one; one run always gets the same one.
    """
    return temp_root / f"lane-{_digest(str(run_dir))}"


def lane_scratch_dir(run_dir: Path, phase: str, task: str) -> Path:
    """One directory per phase and task, named for a digest of the pair, so two pairs never share one."""
    return run_dir / "pytest-tmp" / _digest(f"{phase}--{task}")


def lane_tmp_env(scratch: Path) -> dict[str, str]:
    """The child environment's temp directory: TMPDIR only, so no pytest option is added for the suite to pass on."""
    return {TMPDIR_VAR: str(scratch)}
