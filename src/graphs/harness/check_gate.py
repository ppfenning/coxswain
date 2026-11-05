"""Which checks run at which gate, and the per-repo lock level and ticket checks share.

A check belongs to one gate: `task` reads `checks`, `level` reads
`level_checks`, `ticket` reads `ticket_checks`. A repo that sets neither new list
sees only the task gate, with the list `checks` holds today. The resolvers are
pure. `repo_gate_lock` is the edge: level and ticket check runs take it so two
runs against one repository never overlap. Task checks never take it.
"""

from __future__ import annotations

import fcntl
import subprocess
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

GATES: tuple[str, ...] = ("task", "level", "ticket")

_GATE_KEYS: Mapping[str, str] = {
    "task": "checks",
    "level": "level_checks",
    "ticket": "ticket_checks",
}

_LOCK_NAME = "coxswain-check-gate.lock"


def gate_checks(landing_areas: Mapping[str, object], gate: str) -> list[dict]:
    """The `{name, cmd}` entries for `gate`; a missing, None or non-list value is empty."""
    if gate not in _GATE_KEYS:
        raise ValueError(f"unknown gate {gate!r}; expected one of {GATES}")
    value = landing_areas.get(_GATE_KEYS[gate])
    return value if isinstance(value, list) else []


def gate_commands(landing_areas: Mapping[str, object]) -> dict[str, list[str]]:
    """Command strings per gate, in gate order, with empty gates omitted."""
    by_gate = {g: [c["cmd"] for c in gate_checks(landing_areas, g)] for g in GATES}
    return {g: cmds for g, cmds in by_gate.items() if cmds}


_thread_locks: dict[Path, threading.Lock] = {}
_registry_lock = threading.Lock()


def _common_dir(repo: Path) -> Path:
    out = subprocess.run(
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
