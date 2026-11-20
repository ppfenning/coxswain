"""Pure: which tasks the chair's revert of a phase quarantines, and the cause that quarantine leaves on each row.

A revert takes a phase's land back out of main, so the tasks it quarantines are the ones that land carried: approved or
done (`core.landing_record.SETTLED_TASK_STATES` less `dropped`, which a land never carries). A dropped task, a ready one,
or one building again under a live lane was not part of it and is left as it was.

The store's set-state has no cause, and the chair otherwise reads a quarantine's cause from the task's newest attempt
(`chair_facts.newest_cause`): a run record from before the land, or none. A reverted phase would then read as an old
code failure, sent to triage and back to ready while main is still red, or as a cause-less quarantine for a person, and
the main_red recovery would never fire. So the revert marks each row's `extra` with its cause (`MARK_KEY`), and the
newest run of the initiative when it did. An attempt from a later run is newer news than the revert and wins over it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import NamedTuple

from agent_tools.chair_remedy import set_state_argv
from core.jsontypes import is_mapping

QUARANTINE_FROM = ("approved", "done")
MARK_KEY = "quarantine_mark"  # extra key: {"cause", "reason", "run"} the chair's own quarantine wrote


def _run_number(run: str) -> int:
    match = re.search(r"-(\d+)$", run)
    return int(match.group(1)) if match else -1


def phase_targets(rows: Iterable[Mapping[str, object]], initiative: str, phase: str) -> list[Mapping[str, object]]:
    """The task rows of `initiative`'s `phase` a revert quarantines: those in a state the land carried, in input order."""
    return [
        r for r in rows
        if r.get("kind", "task") == "task" and r.get("initiative") == initiative and r.get("phase") == phase
        and r.get("state") in QUARANTINE_FROM
    ]


class Quarantined(NamedTuple):
    """What the quarantine of a reverted phase did: the tasks it moved, and one line per write the store refused."""

    moved: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()


def newest_run(run_ids: Iterable[str], initiative: str) -> str:
    """The highest-numbered `<initiative>-N` run id; "" when the initiative has none."""
    own = [r for r in run_ids if re.fullmatch(rf"{re.escape(initiative)}-\d+", r)]
    return max(own, key=_run_number, default="")


def mark_run(run_ids: Iterable[str], initiative: str, attempt_runs: Iterable[str]) -> str:
    """The run a task's mark names: the newest of the initiative's recorded runs and the task's own attempts' runs, so an
    attempt that already existed when the mark was made never outdates it, even with no run recorded in the store."""
    return newest_run([*run_ids, *attempt_runs], initiative)


def release_commands(initiative: str, tasks: Iterable[str]) -> list[str]:
    """The hand move that releases each quarantined task to build again, once a person has seen main green. Each names
    the initiative, so a task id another initiative also uses releases this one's ticket."""
    return [" ".join(set_state_argv(initiative, task, "ready")) for task in tasks]


def marked_extra(extra: Mapping[str, object] | None, cause: str, reason: str, run: str) -> dict[str, object]:
    """`extra` with the quarantine's mark set; every other key kept."""
    return {**(extra or {}), MARK_KEY: {"cause": cause, "reason": reason, "run": run}}


def row_mark(extra: Mapping[str, object] | None) -> dict[str, str] | None:
    """The mark a row's `extra` carries, when it is a well-formed one."""
    mark = (extra or {}).get(MARK_KEY)
    if not is_mapping(mark):
        return None
    cause = mark.get("cause")
    if not isinstance(cause, str) or not cause:
        return None
    return {"cause": cause, "reason": str(mark.get("reason") or ""), "run": str(mark.get("run") or "")}


def marked_cause(mark: Mapping[str, str] | None, attempts: Sequence[Mapping[str, object]]) -> Mapping[str, str] | None:
    """The mark, while no attempt of the task comes from a run newer than the one the mark names; else None."""
    if mark is None:
        return None
    after = _run_number(mark.get("run", ""))
    newer = any(_run_number(str(a.get("run") or "")) > after for a in attempts)
    return None if newer else mark
