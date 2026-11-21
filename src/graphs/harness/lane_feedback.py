"""Why a task's last attempt was refused, for the build that tries it again. Pure.

On the phase model a task's build has no fix loop (`lifecycle_propose`'s build-only run): a handoff that finds the
change under-evidenced, or task checks that fail, end the attempt, and the lane builds the task afresh on its next
claim. Without the reasons that builder repeats the omission (E1, 2026-11-21: three attempts refused for the same
missing evidence). So the lane reads the task's refused steps of the phase's current episode
(`store_phases.task_refusals`: its `handoff` and `checks` rows) and adds `refusal_feedback`'s text to the ticket body,
which the plan and the build both read, as the old model's fix loop hands a rebuild the standing critique inside one
run. The handoff's own `missing` items reach the builder verbatim, in the fix loop's words
(`lifecycle_propose._handoff_critique`, `_critique`).

Only the latest attempt that refused the work counts: a harness fault (a check that timed out or could not run) says
nothing about the change, and an older refusal was the input to the attempt that came after it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from graphs.delivery.lifecycle_propose import _critique, _handoff_critique

HEADING = "## Your last attempt was refused"
_FAULTS = frozenset({"timed_out", "unrunnable"})  # `checks.quarantine_reason`'s harness faults: not the change's


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _strings(value: object) -> list[str]:
    return [str(v) for v in value if str(v).strip()] if isinstance(value, list) else []


def _handoff_missing(row: Mapping[str, Any]) -> list[str]:
    result = _mapping(row.get("result_json"))
    return [] if result.get("complete") is True else _strings(result.get("missing"))


def _failed_checks(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    checks = _mapping(row.get("result_json")).get("checks")
    listed = [_mapping(c) for c in checks] if isinstance(checks, list) else []
    return [c for c in listed if c.get("passed") is not True and str(c.get("outcome") or "") not in _FAULTS]


def _refuses(row: Mapping[str, Any]) -> bool:
    kind = row.get("kind")
    return bool(_handoff_missing(row)) if kind == "handoff" else kind == "checks" and bool(_failed_checks(row))


def _check_line(check: Mapping[str, Any]) -> str:
    return (f"- check `{check.get('name')}` failed (exit {check.get('exit_code')}): run it in your session and change "
            "what it reports, then attach the command and its real output")


def refusal_feedback(rows: Sequence[Mapping[str, Any]]) -> str:
    """Pure. The text a task's next build is given from its earlier step rows (`run_id`, `kind`, `result_json`,
    `finished_at`; any order): the latest refusing run's handoff `missing` items and failing checks, under `HEADING`.
    "" when no row refused the change."""
    refusing = [r for r in rows if _refuses(r)]
    if not refusing:
        return ""
    last = max(refusing, key=lambda r: (str(r.get("finished_at") or ""), str(r.get("run_id") or "")))
    run = str(last.get("run_id") or "")
    of_run = [r for r in refusing if str(r.get("run_id") or "") == run]
    missing = [m for r in of_run if r.get("kind") == "handoff" for m in _handoff_missing(r)]
    failed = [c for r in of_run if r.get("kind") == "checks" for c in _failed_checks(r)]
    said = [_critique(_handoff_critique({"missing": missing})[0], None, None)] if missing else []
    return "\n".join([
        HEADING,
        f"An earlier build of this task (run {run}) was refused for the reasons below, and this build starts again "
        "from the phase tip. Answer every one of them in this build: a change, or a command you ran with its real "
        "output. A build that leaves one standing is refused again.",
        *said,
        *(_check_line(c) for c in failed),
    ])


def with_feedback(body: str, feedback: str) -> str:
    """Pure. The ticket body a task's build reads: `body`, then `feedback` when there is any."""
    return "\n\n".join(part for part in (body, feedback) if part)
