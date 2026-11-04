"""The land sink that runs each land as its own detached process, so a land outlives the tick and the chair.

State lives on disk, one spec file per running land and one outcome file once it ends, so a land started by an
earlier chair process is still counted and collected. Every edge is injected; this class takes no lease, because
the land command takes the per-repo land lease under its own holder id.
"""

from __future__ import annotations

import dataclasses
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from agent_tools.chair_land import UNPAUSED
from agent_tools.chair_land_proc import (
    LandOutcome,
    LandSpec,
    _write_atomic,
    count_by_repo,
    dead_outcome,
    encode_spec,
    land_key,
    parse_outcome,
    parse_spec,
    paths_for,
)

if TYPE_CHECKING:
    from agent_tools.chair_exec import Action, Result
    from agent_tools.ci_gate import CiGate

LAND_TIMEOUT_S = 45 * 60.0  # mirrors chair_exec.LAND_TIMEOUT_S: the bound the child puts on one `cox runs land`


class ThreadSink(Protocol):
    """The existing thread sink: lands that cannot run as a process, and the gate and stop they honour."""

    def set_gate(self, gate: CiGate) -> None: ...
    def pending(self, repo: str) -> bool: ...
    def landing(self) -> list[dict[str, str]]: ...
    def submit(self, action: Action, work: Callable[[], Result]) -> Result: ...
    def collect(self) -> list[Result]: ...
    def stop(self) -> None: ...


def _specs(state_dir: Path) -> list[LandSpec]:
    """Every readable spec in `state_dir`, oldest name first; an unreadable file is skipped."""
    found = (_read_spec(p) for p in sorted(state_dir.glob("*.spec.json")))
    return [s for s in found if s is not None]


def _read_spec(path: Path) -> LandSpec | None:
    try:
        return parse_spec(path.read_text())
    except OSError:
        return None


def _read_outcome(path: Path) -> LandOutcome | None:
    try:
        return parse_outcome(path.read_text())
    except OSError:
        return None


def _in_progress(action: Action, reason: str) -> Result:
    return {"action": action, "status": "in_progress", "reason": reason}


class ProcessLands:
    """Edge. A land is a spec file plus a detached `chair_land_proc` process; `collect` turns each ended one into a Result once."""

    def __init__(
        self,
        state_dir: Path,
        fallback: ThreadSink,
        finish: Callable[[Action, int, str], Result],
        limit_for: Callable[[str], int],
        spawn: Callable[[list[str]], int],
        alive: Callable[[int], bool],
        host: str,
        now: Callable[[], float],
    ) -> None:
        self._dir = state_dir
        self._fallback = fallback
        self._finish = finish
        self._limit_for = limit_for
        self._spawn = spawn
        self._alive = alive
        self._host = host
        self._now = now
        self._gate: CiGate = UNPAUSED

    def set_gate(self, gate: CiGate) -> None:
        self._gate = gate
        self._fallback.set_gate(gate)

    def start(self, action: Action, argv: list[str]) -> Result:
        """Write the spec, spawn the land in a new session, record its pid. A held or already running land spawns nothing."""
        key = land_key(action)
        spec_path, outcome_path = paths_for(self._dir, key)
        if spec_path.exists():
            return _in_progress(action, f"land already running: {key}")
        if self._gate.paused:
            return _in_progress(action, f"land held: {self._gate.reason or 'gate paused'}")
        self._dir.mkdir(parents=True, exist_ok=True)
        outcome_path.unlink(missing_ok=True)  # a leftover from a crashed collect must not answer for this land
        spec = LandSpec(key, action, argv, action.get("repo", ""), LAND_TIMEOUT_S, self._now(), self._host, 0)
        _write_atomic(spec_path, encode_spec(spec))
        try:
            pid = self._spawn([sys.executable, "-m", "agent_tools.chair_land_proc", str(spec_path)])
        except OSError as exc:
            spec_path.unlink(missing_ok=True)
            return {
                "action": action,
                "status": "failed",
                "reason": f"land could not start: {type(exc).__name__}: {exc}",
            }
        _write_atomic(spec_path, encode_spec(dataclasses.replace(spec, pid=pid)))
        return _in_progress(action, f"landing in {spec.repo}")

    def _gone(self, spec: LandSpec) -> bool:
        """True when the land's process is known dead. A spec with no recorded pid is pending until its timeout passes.

        `start` records the pid after the spawn, so a crash in that window leaves a running land with pid 0; it is
        not dead yet, and pid 0 must never reach `alive` (`os.kill(0, 0)` always succeeds).
        """
        if spec.pid <= 0:
            return self._now() - spec.started_at > spec.timeout_s
        return spec.host == self._host and not self._alive(spec.pid)

    def _settled(self, spec: LandSpec) -> LandOutcome | None:
        _, outcome_path = paths_for(self._dir, spec.key)
        found = _read_outcome(outcome_path)
        if found is not None or not self._gone(spec):
            return found
        # The process may have written its outcome and exited between the read above and the liveness check.
        return _read_outcome(outcome_path) or dead_outcome(spec, spec.pid)

    def collect(self) -> list[Result]:
        """Each ended land once: its files are deleted after it is reported."""
        results: list[Result] = []
        for spec in _specs(self._dir):
            outcome = self._settled(spec)
            if outcome is None:
                continue
            results.append(self._finish(spec.action, outcome.code, outcome.output))
            spec_path, outcome_path = paths_for(self._dir, spec.key)
            spec_path.unlink(missing_ok=True)  # the spec first: a spec with no outcome is read as dead
            outcome_path.unlink(missing_ok=True)
        return [*results, *self._fallback.collect()]

    def _count(self, repo: str) -> int:
        on_disk = count_by_repo(_specs(self._dir)).get(repo, 0)
        return on_disk + sum(1 for entry in self._fallback.landing() if entry.get("repo") == repo)

    def pending(self, repo: str) -> bool:
        return self._count(repo) > 0 or self._fallback.pending(repo)

    def full(self, repo: str) -> bool:
        return self._count(repo) >= self._limit_for(repo)

    def landing(self) -> list[dict[str, str]]:
        from_disk = [
            {
                "initiative": s.action.get("initiative", ""),
                "phase": s.action.get("phase", ""),
                "repo": s.repo,
            }
            for s in _specs(self._dir)
        ]
        return [*from_disk, *self._fallback.landing()]

    def held_here(self) -> list[dict[str, str]]:
        """The lands that die with this chair: only the fallback's."""
        return self._fallback.landing()

    def submit(self, action: Action, work: Callable[[], Result]) -> Result:
        return self._fallback.submit(action, work)

    def stop(self) -> None:
        """Stops the fallback only. A land process is never signalled."""
        self._fallback.stop()
