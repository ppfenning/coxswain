"""Which tasks of one initiative can launch now, and what blocks it when none can.

Pure. Takes plain task data and the ordered phase list; no file, store or clock access.
"""

from dataclasses import dataclass

from core.landing import DEFAULT_LAND_AT, phase_counts_complete
from core.phase_hold import phase_holds

READY = "ready"
DONE = "done"
QUARANTINED = "quarantined"
APPROVED = "approved"  # approved and not yet landed; a landed task is `done`
MERGED = "merged"  # approved and its PR merged into the level branch; complete unless land_at is `task`


@dataclass(frozen=True)
class TaskView:
    id: str
    phase: str
    state: str
    needs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Blocking:
    """The earliest phase still holding quarantined or approved-unlanded tasks, with both counts."""

    phase: str
    quarantined: int
    awaiting_land: int


@dataclass(frozen=True)
class WaitingInitiative:
    """The action the plan emits for an initiative that has nothing runnable and is blocked."""

    initiative: str
    blocking: Blocking


@dataclass(frozen=True)
class Runnable:
    """Runnable task ids in input order. An empty tuple means nothing is runnable and nothing blocks."""

    task_ids: tuple[str, ...]


def _merged_counts(land_at: str, task: TaskView) -> bool:
    """True for a merged task that `core.landing.phase_counts_complete` accepts as complete at `land_at`."""
    return task.state == MERGED and phase_counts_complete(land_at, [("approved", True)])


def _blocking_phases(tasks: tuple[TaskView, ...], phases: tuple[str, ...], land_at: str) -> tuple[Blocking, ...]:
    """Blocking phases in `phases` order; a phase with neither quarantined nor awaiting-land tasks is absent.

    A merged task awaits its land only where `land_at` does not count it complete."""
    return tuple(
        Blocking(phase, quarantined, awaiting_land)
        for phase in phases
        for quarantined, awaiting_land in (
            (
                sum(1 for t in tasks if t.phase == phase and t.state == QUARANTINED),
                sum(1 for t in tasks if t.phase == phase and (t.state == APPROVED or (t.state == MERGED and not _merged_counts(land_at, t)))),
            ),
        )
        if quarantined or awaiting_land
    )


def runnable_verdict(tasks: tuple[TaskView, ...], phases: tuple[str, ...], land_at: str = DEFAULT_LAND_AT) -> Runnable | Blocking:
    """Runnable ids if any task is ready, has all needs done and no earlier blocking phase; else the earliest Blocking, else an empty Runnable."""
    order = {phase: n for n, phase in enumerate(phases)}
    blocking = _blocking_phases(tasks, phases, land_at)
    done = frozenset(t.id for t in tasks if t.state == DONE or _merged_counts(land_at, t))
    met = tuple(t for t in tasks if t.state == READY and all(need in done for need in t.needs))
    first = blocking[0] if blocking else None
    held = first is not None and phase_holds(first.awaiting_land, sum(1 for t in met if t.phase == first.phase))
    cutoff = order[first.phase] - (1 if held else 0) if first else len(phases)
    ids = tuple(t.id for t in met if order.get(t.phase, len(phases)) <= cutoff)
    if ids:
        return Runnable(ids)
    return blocking[0] if blocking else Runnable(())
