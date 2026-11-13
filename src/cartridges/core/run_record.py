"""What one run task record says about an attempt at its task. Pure: no I/O, no clock.

The epic saves every task's lifecycle result as `runs/<run>/tasks/<phase>/<task>.json` (`harness.resume.result_path`)
and mirrors it into the store's `task_records`. Under a store, a quarantine also puts its cause on that record, as
`cause` and `cause_why` (`harness.epic._record_task_cause`). When the board lives in the store, nothing writes the
attempt into a work file, so the record is where an attempt's cause, reason and kind are read back from: by the chair
for its quarantine facts, and by the epic for the attempts it hands triage.

A record that names no cause is no attempt: an approved build, or a run that never quarantined the task.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.workstore import body_sha

RULE_MARK = "rule: "  # the driver's mark on a cause a rule made (`harness.epic._cause_of`, `harness.rescue_select`)
_KIND_RULE = "rule: kind "
_RESCUE_RULE = "rule: rescue "
RESCUE_KIND = "rescue_failed"
_APPROVED_KIND = "draft_pr_create"  # the proposal `lifecycle-propose` emits when, and only when, its reviewers approved


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str:
    return str(value).strip() if isinstance(value, str) else ""


def _last_line(text: object) -> str:
    return next((line.strip() for line in reversed(_text(text).splitlines()) if line.strip()), "")


def record_kind(cause_why: object) -> str:
    """The attempt kind a rule-made cause names: `rule: kind <kind>` names it, `rule: rescue ...` is a failed rescue.
    "" when a model or a person made the cause, which names no kind."""
    why = _text(cause_why)
    if why.startswith(_KIND_RULE):
        return next(iter(why.removeprefix(_KIND_RULE).split()), "")
    return RESCUE_KIND if why.startswith(_RESCUE_RULE) else ""


def _failed_checks(record: Mapping[str, Any]) -> str:
    """Each configured check that failed on the build (`change_facts.checks`), with its exit and the last line it printed."""
    checks = _mapping(record.get("change_facts")).get("checks") or []
    failing = [c for c in checks if isinstance(c, Mapping) and not c.get("passed")]
    return "; ".join(
        f"check {c.get('name')} failed (exit {c.get('exit_code')})" + (f": {tail}" if (tail := _last_line(c.get("output_tail"))) else "")
        for c in failing
    )


def _approved(record: Mapping[str, Any]) -> bool:
    return any(isinstance(p, Mapping) and p.get("kind") == _APPROVED_KIND for p in record.get("proposals") or [])


def _fix_loop(record: Mapping[str, Any]) -> str:
    """Why the fix loop refused the build, in the reviewers' own words: empty for an approved build."""
    if _approved(record):
        return ""
    stopped = _text(_mapping(record.get("fix_loop")).get("stopped"))
    objections = _mapping(record.get("adversary")).get("objections") or []
    claim = next((_text(o.get("claim")) for o in objections if isinstance(o, Mapping) and _text(o.get("claim"))), "")
    said = _text(_mapping(record.get("arbitration")).get("reasoning")) or claim or _text(_mapping(record.get("review")).get("rationale"))
    if not stopped and not said:
        return ""
    return f"the fix loop stopped: {stopped or 'the reviewers did not approve the build'}" + (f"; {said}" if said else "")


def record_reason(record: Mapping[str, Any]) -> str:
    """Why the attempt failed: the cause's own why when a model or a person gave one, then what the record itself says
    failed (a node that raised, a configured check, the fix loop's refusal). A rule's mark alone when nothing else does."""
    why = _text(record.get("cause_why"))
    told = (
        (f"node {record.get('failed_node')} failed: {_text(record.get('failed_node_reason'))}" if _text(record.get("failed_node_reason")) else "")
        or _failed_checks(record)
        or _fix_loop(record)
    )
    judged = why if why and not why.startswith(RULE_MARK) else ""
    return "; ".join(part for part in (judged, told) if part) or why


def record_attempt(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """`{cause, cause_why, reason}` plus `kind` when a rule names it, for a record that names a cause; None otherwise."""
    if not isinstance(record, Mapping) or not _text(record.get("cause")):
        return None
    kind = record_kind(record.get("cause_why"))
    return {
        "cause": _text(record.get("cause")),
        "cause_why": _text(record.get("cause_why")),
        "reason": record_reason(record),
        **({"kind": kind} if kind else {}),
    }


def record_body_sha(record: Mapping[str, Any]) -> str | None:
    """The `body_sha` of the ticket body the record was built on, when the record carries it (`body_sha`, or the
    `ticket_body` text); None when it carries neither, so the record cannot be told apart from the current body."""
    stamped = _text(record.get("body_sha"))
    if stamped:
        return stamped
    body = record.get("ticket_body")
    return body_sha(body) if isinstance(body, str) and body.strip() else None
