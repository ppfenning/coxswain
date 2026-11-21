"""The tick's per-loop chores, run by the queue workers once no tick does (C6).

The tick did more each pass than plan and perform. Before its plan it published the usage meter, pulled the workspace
(when `chair.workspace_git`, on by default) and synced each repo's checkout, and asked the lane hosts whether their
checkouts are clean. Beside its plan it applied the chat directives (`ChatDeps`) and recorded the CI gate's pause line.
After its perform it exported the board (the export fills the store from the files first, which is how an intake filed
as a file, or pulled in from the workspace's remote, becomes a store row the decompose worker sees), and it left the
plan's why-not-launched reasons beside the runs. The cutover stops the tick, so these are the workers' now:

- **housekeeping** owns every chore but the reasons: the meter, the pull and checkout sync, the lane checkout check,
  the chat directives, the CI-gate pause line and the export;
- **launch** writes the reasons file, since only its plan has the `unlaunched` lines;
- **watch**, which owns the land slot, collects the lands that ended on a pass that performed nothing: the tick's
  perform collected them every tick, and a worker performs only when it has something planned.

Each runs only on a live pass of a listed kind while no tick holds the chair lease (`chair_queue.chores_due`), and
each is idempotent: a pull of an up-to-date workspace does nothing, a lane checkout line already recorded is not
recorded again, a chat entry is acked once applied, and the export runs only when the store's version or the
workspace's HEAD moved since the last one this worker made (or a row this pass changed), so a quiet pass costs one
version probe. Each reads only the workspace and the store, never a tick's state, so none needs a tick running.

Edge: these call the tick's own steps (`chair_run`), on the worker's own `RunDeps`.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from agent_tools import chair_run, workspace_lock
from agent_tools.chair_exec import Result
from agent_tools.chair_queue import Chores, ChoreStep
from agent_tools.chair_types import Action, Facts

__all__ = ["ExportState", "export_due", "housekeeping_chores", "launch_chores", "watch_chores", "workspace_head"]


@dataclass
class ExportState:
    """Edge state: the store version and workspace HEAD the last export this worker made was written from."""

    stamp: object | None = None
    head: str | None = None
    exported: bool = False
    busy: bool = False  # the last export was skipped on the workspace lock (`workspace_lock`), so the next is due


def export_due(state: ExportState, stamp: object | None, head: str | None, changed: Sequence[str]) -> bool:
    """Pure. Whether the export runs this pass: the first pass, a row this pass changed, or the store's version or the
    workspace's HEAD (a pull brought commits) moved since the last export, or the last was skipped on the workspace
    lock. An unreadable version is a move."""
    return not state.exported or state.busy or bool(changed) or stamp is None or stamp != state.stamp or head != state.head


def workspace_head(workspace: Path) -> str | None:
    """Edge. The workspace's HEAD commit, None when it is not a git checkout or git does not answer."""
    try:
        done = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


@dataclass
class _Pass:
    """Edge state between one pass's `before` and `after`."""

    lane: chair_run.LaneTick | None = None
    paused: bool | None = None
    pulled: str = ""
    notes: list[str] = field(default_factory=list[str])


def housekeeping_chores(
    deps: chair_run.RunDeps, stamp: Callable[[], object | None], head: Callable[[], str | None]
) -> Chores:
    """Edge. Housekeeping's chores on the worker's `deps`; `stamp` is the store's queue version, `head` the workspace's
    HEAD. In the tick's order: the meter, the pull, the lane checkouts, the gate, the chat; then, after the perform,
    the lane lines settled and the export."""
    state = ExportState()
    current = _Pass()

    def before(facts: Facts, planned: Sequence[Action], now: datetime) -> ChoreStep:
        current.lane, current.paused, current.notes = None, None, []
        chair_run.publish_meter(deps, False, now)
        current.pulled = chair_run.pull_step(deps, False)
        current.lane = chair_run.lane_checkout_step(deps, False)
        gate = chair_run.gate_now(deps, now)
        notice = chair_run.next_gate_record(deps.gate_state.paused, gate, (), 0) if gate.paused else None
        current.paused = gate.paused
        chat = chair_run.directive_step(deps, True, facts, planned, now)
        notes = [chat, f"workspace pull failed: {current.pulled}" if current.pulled else ""]
        fresh = [*current.lane.fresh, *([notice] if notice is not None else [])]
        standing = [a for a in current.lane.standing if a not in current.lane.fresh]
        return ChoreStep(perform=fresh, push=standing, note=" | ".join(n for n in notes if n))

    def after(actions: Sequence[Action], results: Sequence[Result]) -> str:
        if current.lane is not None:
            chair_run.settle_lane_sync(deps, current.lane, list(results))
        if current.paused is not None:
            deps.gate_state.paused = current.paused
        if deps.export_rows is None:
            return ""
        ids = chair_run.changed_row_ids(results)
        now_stamp, now_head = stamp(), head()
        if not export_due(state, now_stamp, now_head, ids):
            return ""
        note = chair_run.export_note(deps.export_rows, ids)
        state.busy = workspace_lock.skipped(note)  # another writer held the workspace past the bound
        if state.busy:  # retried on the next pass, not on the next move
            return note
        # The export's own fill and commit move both, so read them again: the next quiet pass must find no change. A
        # failed export is retried on the next move, as the tick retried it on its next changed row, not every pass.
        state.stamp, state.head, state.exported = stamp(), head(), True
        return note

    return Chores(before=before, after=after)


def launch_chores(deps: chair_run.RunDeps) -> Chores:
    """Edge. The launch worker's one chore: the why-not-launched reasons of its plan, beside the runs, as the tick
    left them (`chair_run.publish_reasons`)."""

    def before(_facts: Facts, _planned: Sequence[Action], _now: datetime) -> ChoreStep:
        return ChoreStep()

    def after(actions: Sequence[Action], _results: Sequence[Result]) -> str:
        deps.reason_state.items = [
            {"initiative": a.get("initiative"), "reason": a.get("reason")} for a in actions if a.get("kind") == "unlaunched"
        ]
        chair_run.publish_reasons(deps, False, datetime.now(UTC))
        return ""

    return Chores(before=before, after=after)


def watch_chores(deps: chair_run.RunDeps) -> Chores:
    """Edge. The watch worker's one chore: on a pass that performed nothing, collect the lands that ended since the last
    perform (`chair_exec.perform` with no actions), so a finished land is recorded within one pass, as on the tick.
    A pass that performed an action has already collected them."""

    def before(_facts: Facts, _planned: Sequence[Action], _now: datetime) -> ChoreStep:
        return ChoreStep()

    def after(actions: Sequence[Action], _results: Sequence[Result]) -> str:
        if actions or deps.exec_deps.lands is None:
            return ""
        collected = deps.perform([], deps.exec_deps, lambda: -1, False)
        return f"collected {len(collected)} ended land(s)" if collected else ""

    return Chores(before=before, after=after)
