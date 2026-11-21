"""Pure decompose-miss streak: has an intake hit EMPTY_DECOMPOSE_LIMIT. No file, process or clock access.

A decompose run misses when it wrote no task items, or when the tasks it wrote collided: a lane's `collision` step
row (L5, `lane_merge.collision_row`) says two tasks the decompose called independent edited the same files. Each
collision counts against the decompose run of its initiative launched last before the collision finished
(`with_collisions`), so a later decompose that splits the work honestly starts clean."""
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from itertools import takewhile

from core.jsontypes import is_list, is_mapping

EMPTY_DECOMPOSE_LIMIT = 2


@dataclass(frozen=True)
class DecomposeRun:
    run_id: str
    wrote_items: bool
    refusal_line: str | None  # first "approved but not executed" log line, None when the run printed none
    launched_at: datetime | None = None  # timezone-aware; None when the store row gave no readable time
    collisions: tuple[str, ...] = ()  # the collisions its tasks hit, each `<phase>/<task>~<other>: <files>`
    exited: bool = True  # False while its `runs` row has no end (`chair_read_exits.row_exited`)
    ended_at: datetime | None = None  # timezone-aware; None when it has not ended or the row gave no readable time


def missed(run: DecomposeRun) -> bool:
    """A run that wrote nothing, or whose tasks collided."""
    return not run.wrote_items or bool(run.collisions)


@dataclass(frozen=True)
class DecomposeStreak:
    empty_count: int  # trailing missed runs: empty or collided
    run_ids: tuple[str, ...]  # oldest first
    first_refusal: str | None
    collisions: tuple[str, ...] = ()  # the collisions of those runs, oldest first


def counted(run: DecomposeRun, since: datetime | None) -> bool:
    """A run launched before `since`, the intake's last release or requeue, is not counted; one with no launch time is."""
    return since is None or run.launched_at is None or run.launched_at >= since


def streak(runs: Sequence[DecomposeRun], since: datetime | None = None) -> DecomposeStreak:
    """Trailing runs that missed (`missed`), among those `counted` against `since`; `runs` is ordered oldest first."""
    kept = [r for r in runs if counted(r, since)]
    tail = tuple(reversed(tuple(takewhile(missed, reversed(kept)))))
    return DecomposeStreak(
        empty_count=len(tail),
        run_ids=tuple(r.run_id for r in tail),
        first_refusal=next((r.refusal_line for r in tail if r.refusal_line is not None), None),
        collisions=tuple(c for r in tail for c in r.collisions),
    )


def collision_label(row: Mapping[str, object]) -> str:
    """One collision step row as `<phase>/<task>~<other>: <files>`, from its result (`lane_merge.collision_row`)."""
    result = row.get("result_json")
    found: Mapping[str, object] = result if is_mapping(result) else {}
    files = found.get("files")
    shared = ", ".join(str(f) for f in files) if is_list(files) else ""
    task, other = str(found.get("task") or row.get("task_id") or ""), str(found.get("other") or "")
    return f"{row.get('phase') or ''}/{task}~{other}: {shared}"


def with_collisions(runs: Sequence[DecomposeRun], rows: Sequence[Mapping[str, object]]) -> list[DecomposeRun]:
    """`runs` (one initiative's decompose runs, oldest first) with each collision step row of that initiative put on
    the run launched last at or before the collision finished. A collision before every run, or with no readable time,
    goes on the newest run."""
    if not runs or not rows:
        return list(runs)
    own: dict[int, list[str]] = {}
    for row in rows:
        at = _when(row.get("finished_at"))
        before = [n for n, r in enumerate(runs) if at is not None and r.launched_at is not None and r.launched_at <= at]
        own.setdefault(before[-1] if before else len(runs) - 1, []).append(collision_label(row))
    return [replace(r, collisions=(*r.collisions, *own.get(n, []))) for n, r in enumerate(runs)]


def _when(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def at_limit(s: DecomposeStreak) -> bool:
    return s.empty_count >= EMPTY_DECOMPOSE_LIMIT


def stalled_reason(path: str, s: DecomposeStreak) -> str:
    """The needs_chair line for an intake whose decompose streak is at the limit: how many runs missed, which, and
    the evidence: the first refusal line of an empty run, and every collision a run's tasks hit."""
    if not s.collisions:
        return (f"decompose of {path} wrote no task items in {len(s.run_ids)} runs: {', '.join(s.run_ids)}. "
                f"{s.first_refusal or 'no refusal line was printed'}")
    refusal = f" {s.first_refusal}" if s.first_refusal else ""
    return (f"decompose of {path} missed in {len(s.run_ids)} runs: {', '.join(s.run_ids)}; each wrote no task items "
            f"or wrote tasks that collided: {'; '.join(s.collisions)}.{refusal}")
