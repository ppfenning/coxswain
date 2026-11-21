"""Which checks run at which gate, and the per-repo lock level and ticket checks share.

The three gates are the three check levels. `task` is a task's cheap checks: the cartridge's `checks` and the
repository's `.agent-checks` task section. `level` is a phase's smoke, run once its tasks are together: the cartridge's
`level_checks` and the `# level: phase` section. `ticket` is an initiative's full suite, run before its one pull
request: the cartridge's `ticket_checks` and the `# level: initiative` section. A repo that sets neither new list and
marks no section sees only the task gate, with the list `checks` holds today. The resolvers are pure.

Only the phase model reads the sections at their gates (`Ctx.check_levels`, set by the phase lane). The old model's
runs, the lander and `cox runs land` pass no `repo` here: they read the file as one flat task list
(`checks.old_model_checks`), so an initiative already on the old model checks what it checked before levels. A
new-model lander's gate is D1's.
`repo_gate_lock` is the edge: level and ticket check runs take it so two runs against one repository never overlap.
Task checks never take it.
"""

from __future__ import annotations

import fcntl
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from harness import bounded_proc
from harness.checks import for_level

GATES: tuple[str, ...] = ("task", "level", "ticket")
# The `.agent-checks` section each gate reads (`checks.LEVELS`).
GATE_LEVELS: Mapping[str, str] = {"task": "task", "level": "phase", "ticket": "initiative"}

_GATE_KEYS: Mapping[str, str] = {
    "task": "checks",
    "level": "level_checks",
    "ticket": "ticket_checks",
}

_LOCK_NAME = "coxswain-check-gate.lock"


def gate_checks(landing_areas: Mapping[str, object], gate: str,
                repo: Sequence[Mapping[str, object]] = ()) -> list[dict[str, str]]:
    """The `{name, cmd}` entries for `gate`: the cartridge's list (a missing, None or non-list value is empty), then
    the entries of the repository's parsed `.agent-checks` (`checks.repo_checks`) for the gate's level whose `cmd` the
    cartridge does not already run."""
    if gate not in _GATE_KEYS:
        raise ValueError(f"unknown gate {gate!r}; expected one of {GATES}")
    value = landing_areas.get(_GATE_KEYS[gate])
    listed = cast("list[dict[str, str]]", value) if isinstance(value, list) else []
    known = {c.get("cmd") for c in listed}
    extra = [cast("dict[str, str]", c) for c in for_level(repo, GATE_LEVELS[gate]) if c.get("cmd") not in known]
    return listed + extra if extra else listed  # nothing added: the cartridge's own list, as before levels


def gate_commands(landing_areas: Mapping[str, object],
                  repo: Sequence[Mapping[str, object]] = ()) -> dict[str, list[str]]:
    """Command strings per gate, in gate order, with empty gates omitted. `repo` adds to the level and ticket gates
    only: the task gate here is the cartridge's list, as the runner has always been given it, since a run joins the
    task lines of `.agent-checks` itself (`Ctx.checks`)."""
    by_gate = {g: [c["cmd"] for c in gate_checks(landing_areas, g, repo if g != "task" else ())] for g in GATES}
    return {g: cmds for g, cmds in by_gate.items() if cmds}


_thread_locks: dict[Path, threading.Lock] = {}
_registry_lock = threading.Lock()


def _common_dir(repo: Path) -> Path:
    out = bounded_proc.completed(
        ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return (Path(repo) / out).resolve()


def _thread_lock(common: Path) -> threading.Lock:
    with _registry_lock:
        return _thread_locks.setdefault(common, threading.Lock())


@contextmanager
def repo_gate_lock(repo: Path) -> Iterator[None]:
    """Hold the repository's exclusive gate lock for the with block; level and ticket runs only."""
    common = _common_dir(Path(repo))
    with _thread_lock(common), open(common / _LOCK_NAME, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
