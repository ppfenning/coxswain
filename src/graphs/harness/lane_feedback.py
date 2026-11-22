"""Why a task's last attempt was refused, for the build that tries it again. Pure.

On the phase model a task's build has no fix loop (`lifecycle_propose`'s build-only run): a handoff that finds the
change under-evidenced, or task checks that fail, end the attempt, and the lane builds the task afresh on its next
claim. Without the reasons that builder repeats the omission (E1, 2026-11-21: three attempts refused for the same
missing evidence). So the lane reads the task's attempts of the phase's current episode
(`store_phases.task_attempt_rows`: its `handoff` and `checks` rows, passed or failed) and adds `refusal_feedback`'s text to the ticket body,
which the plan and the build both read, as the old model's fix loop hands a rebuild the standing critique inside one
run. The handoff's own `missing` items reach the builder verbatim, in the fix loop's words
(`lifecycle_propose._handoff_critique`, `_critique`). A failed check reaches it with the end of its output, as the
`checks` row kept it (`store_steps._tail`: up to three checks, 2,000 characters each, scrubbed), so the builder sees the very
errors (E1: `check_types`' two pyright errors, which a name and an exit code alone did not name).

Only the latest verdict on the work counts: the last handoff, or the last checks that judged the change. If it passed,
there is nothing to say, so a refusal a later attempt answered never comes back. A harness fault (a check that timed
out or could not run) is no verdict: it says nothing about the change.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from graphs.delivery.lifecycle_review import _critique, _handoff_critique
from harness.store_steps import FAULT_OUTCOMES, TAIL_KEPT_CHECKS

HEADING = "## Your last attempt was refused"


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
    return [c for c in listed if c.get("passed") is not True and str(c.get("outcome") or "") not in FAULT_OUTCOMES]


def _judged(row: Mapping[str, Any]) -> bool:
    """Whether the row is a verdict on the change: any handoff, or checks of which one at least was not a fault."""
    if row.get("kind") == "handoff":
        return True
    checks = _mapping(row.get("result_json")).get("checks")
    listed = [_mapping(c) for c in checks] if isinstance(checks, list) else []
    return row.get("kind") == "checks" and any(str(c.get("outcome") or "") not in FAULT_OUTCOMES for c in listed)


def _refuses(row: Mapping[str, Any]) -> bool:
    kind = row.get("kind")
    return bool(_handoff_missing(row)) if kind == "handoff" else kind == "checks" and bool(_failed_checks(row))


def _fenced(text: str) -> str:
    """`text` in a code fence longer than any run of backticks inside it."""
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", text)), default=0))
    return f"{fence}\n{text}\n{fence}"


def _check_line(check: Mapping[str, Any], *, shown: bool) -> str:
    """One failed check: what it is, then the end of its output as its row kept it (`store_steps._tail`, capped and
    scrubbed before it was stored: `artifacts.scrub_tails`), so the build sees the very errors to fix."""
    line = (f"- check `{check.get('name')}` failed (exit {check.get('exit_code')}): run it in your session and change "
            "what it reports, then attach the command and its real output")
    tail = str(check.get("output_tail") or "").strip() if shown else ""
    return f"{line}. The end of its output:\n{_fenced(tail)}" if tail else line


def _order(row: Mapping[str, Any]) -> tuple[str, str, bool, int]:
    """When the row was written; within one store second and run, a rebuild's after the first attempt's, and an
    attempt's checks after its handoff, which they always follow."""
    return (str(row.get("finished_at") or ""), str(row.get("run_id") or ""), "-rebuild/" in str(row.get("step") or ""),
            1 if row.get("kind") == "checks" else 0)


def refusal_feedback(rows: Sequence[Mapping[str, Any]]) -> str:
    """Pure. The text a task's next build is given from its earlier step rows (`run_id`, `kind`, `result_json`,
    `finished_at`; any order), passed and failed: when the latest verdict on the change (`_judged`) refused it, that
    run's handoff `missing` items and failing checks, under `HEADING`. "" when it passed, or nothing judged the change."""
    judged = [r for r in rows if _judged(r)]
    if not judged:
        return ""
    last = max(judged, key=_order)
    if not _refuses(last):
        return ""
    run = str(last.get("run_id") or "")
    of_run = [r for r in judged if _refuses(r) and str(r.get("run_id") or "") == run]
    missing = [m for r in of_run if r.get("kind") == "handoff" for m in _handoff_missing(r)]
    failed = [c for r in of_run if r.get("kind") == "checks" for c in _failed_checks(r)]
    said = [_critique(_handoff_critique({"missing": missing})[0], None, None)] if missing else []
    return "\n".join([
        HEADING,
        f"An earlier build of this task (run {run}) was refused for the reasons below, and this build starts again "
        "from the phase tip. Answer every one of them in this build: a change, or a command you ran with its real "
        "output. A build that leaves one standing is refused again.",
        *said,
        # One run can hold two checks rows (an attempt's, then its rebuild's): its rows' cap holds for the whole text.
        *(_check_line(c, shown=n < TAIL_KEPT_CHECKS) for n, c in enumerate(failed)),
    ])


def with_feedback(body: str, feedback: str) -> str:
    """Pure. The ticket body a task's build reads: `body`, then `feedback` when there is any."""
    return "\n\n".join(part for part in (body, feedback) if part)
