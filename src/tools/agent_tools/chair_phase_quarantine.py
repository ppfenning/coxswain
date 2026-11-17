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
from typing import Any

QUARANTINE_FROM = ("approved", "done")
MARK_KEY = "quarantine_mark"  # extra key: {"cause", "reason", "run"} the chair's own quarantine wrote


def _run_number(run: str) -> int:
    match = re.search(r"-(\d+)$", run)
    return int(match.group(1)) if match else -1


def phase_targets(rows: Iterable[Mapping[str, Any]], initiative: str, phase: str) -> list[Mapping[str, Any]]:
    """The task rows of `initiative`'s `phase` a revert quarantines: those in a state the land carried, in input order."""
    return [
        r for r in rows
        if r.get("kind", "task") == "task" and r.get("initiative") == initiative and r.get("phase") == phase
        and r.get("state") in QUARANTINE_FROM
    ]


def newest_run(run_ids: Iterable[str], initiative: str) -> str:
    """The highest-numbered `<initiative>-N` run id; "" when the initiative has none."""
    own = [r for r in run_ids if re.fullmatch(rf"{re.escape(initiative)}-\d+", r)]
    return max(own, key=_run_number, default="")


def marked_extra(extra: Mapping[str, Any] | None, cause: str, reason: str, run: str) -> dict[str, Any]:
    """`extra` with the quarantine's mark set; every other key kept."""
    return {**(extra or {}), MARK_KEY: {"cause": cause, "reason": reason, "run": run}}


def row_mark(extra: Mapping[str, Any] | None) -> dict[str, str] | None:
    """The mark a row's `extra` carries, when it is a well-formed one."""
    mark = (extra or {}).get(MARK_KEY)
    if not isinstance(mark, Mapping) or not isinstance(mark.get("cause"), str) or not mark["cause"]:
        return None
    return {"cause": mark["cause"], "reason": str(mark.get("reason") or ""), "run": str(mark.get("run") or "")}


def marked_cause(mark: Mapping[str, str] | None, attempts: Sequence[Mapping[str, Any]]) -> Mapping[str, str] | None:
    """The mark, while no attempt of the task comes from a run newer than the one the mark names; else None."""
    if mark is None:
        return None
    after = _run_number(mark.get("run", ""))
    newer = any(_run_number(str(a.get("run") or "")) > after for a in attempts)
    return None if newer else mark
