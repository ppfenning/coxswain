"""The records the epic driver reads, typed, each with one pure constructor that validates what it was handed (H2).

- `TaskRecord`: a run's task record, the lifecycle result the driver saves as `runs/<run>/tasks/<phase>/<task>.json`
  and mirrors into the store's `task_records.record_json`.
- `AttemptRow`: one row of the store's `attempts` table, as the driver selects it.
- `Attempt`: one attempt on a task, as the driver hands it to triage: the work file's `core.workstore.Attempt`, or one
  rebuilt from a run's task record, with the cause and the verdicts that record carried.
- `PhaseRecord`: what one phase did, as `run_phase_tasks` builds it and the store's `phases` row holds it.

A record is a JSON object, so every value is `Json` at worst; the keys the driver reads are named and typed, and a
record keeps any other key it carries, untouched. A constructor refuses input whose named keys have the wrong type, or
that is not JSON at all, with `MalformedRecord`: the driver reads such a record as no record, the same as one it
could not open. Nothing here does I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, NotRequired, TypedDict, cast

from harness.store_dialect import Json, JsonObject

__all__ = [
    "Attempt",
    "AttemptRow",
    "MalformedRecord",
    "PhaseRecord",
    "TaskRecord",
    "as_json",
    "as_list",
    "as_mapping",
    "attempt",
    "attempt_row",
    "phase_record",
    "task_record",
]

type Kind = Literal["str", "str?", "int", "int?", "bool", "object", "object?", "list", "list?", "json"]


class MalformedRecord(ValueError):
    """A record from the store or a record file whose shape the driver cannot read."""


class TaskRecord(TypedDict, total=False):
    """A run's task record. Only the keys the driver reads are named; a record carries more. A node that did not
    run leaves its key null, so each named key may be."""

    run_id: str | None
    initiative: str | None
    date: str | None
    ts: str | None
    cause: str | None
    cause_why: str | None
    build: JsonObject | None
    evidence: list[Json] | None
    review: JsonObject | None
    adversary: JsonObject | None
    arbitration: Json
    fix_loop: JsonObject | None
    change_facts: JsonObject | None
    proposals: list[Json] | None
    failed_node: str | None
    failed_node_reason: str | None


class AttemptRow(TypedDict):
    """One `attempts` row. Every column the driver selects is named; a select of fewer leaves the rest out. The
    store's text columns are nullable, so each reads as None when unset."""

    run_id: str
    phase_id: str | None
    seq: NotRequired[int]
    kind: NotRequired[str | None]
    reason: NotRequired[str | None]
    ts: NotRequired[str | None]
    cause: str | None


class Attempt(TypedDict):
    """One attempt on a task: the work file's (`core.workstore.Attempt`) or one rebuilt from a run's task record."""

    run: str
    phase: str
    reason: str
    ts: str
    kind: NotRequired[str]
    patch_kept: NotRequired[bool]
    body_sha: NotRequired[str]
    cause: NotRequired[str | None]
    cause_why: NotRequired[str]
    review: NotRequired[JsonObject]
    adversary: NotRequired[JsonObject]
    arbitration: NotRequired[JsonObject]
    fix_loop: NotRequired[JsonObject]
    evidence: NotRequired[list[Json]]
    malformed: NotRequired[str]  # why its task record was refused: an attempt with no verdicts


class PhaseRecord(TypedDict, total=False):
    """What one phase did, as `run_phase_tasks` builds it. `phase` and `status` are always there; the rest as the phase
    got to them. `task_records` are the phase's own per-task entries, not the run task records above."""

    phase: str
    status: str
    parent: str | None
    parents: list[str]
    assembled_from: list[str]
    branch: str
    base: str
    land_at: str
    reason: str | None
    rebased: bool
    task_records: list[dict[str, object]]
    quarantined: list[dict[str, object]]
    batch: list[dict[str, object]]
    cleared_worktrees: list[str]
    reused_branch: bool
    reused_tasks: list[str]
    recreated: str
    refreshed: str
    remote_push_failed: str
    phase_branch_diverged: list[str]
    missing_branch: str
    phase_failed_to_start: str
    awaiting_tasks: list[str]
    base_rebuilt: list[str]
    stopped: str
    interrupted: list[dict[str, object]]
    serialised: object
    initiative_merge: dict[str, object]
    phase_verdict: dict[str, object]
    chunk_verdicts: list[object]
    level_checks: list[dict[str, object]]
    level_integration: list[dict[str, object]]
    level_integration_scope: object
    trim: str
    totals: dict[str, int]
    manifest: str
    manifest_record: dict[str, object]


_TASK_RECORD: dict[str, Kind] = {
    "run_id": "str?", "initiative": "str?", "date": "str?", "ts": "str?", "cause": "str?", "cause_why": "str?",
    "build": "object?", "evidence": "list?", "review": "object?", "adversary": "object?", "arbitration": "json",
    "fix_loop": "object?", "change_facts": "object?", "proposals": "list?", "failed_node": "str?",
    "failed_node_reason": "str?",
}  # fmt: skip
_ATTEMPT_ROW: dict[str, Kind] = {
    "run_id": "str", "phase_id": "str?", "seq": "int", "kind": "str?", "reason": "str?", "ts": "str?", "cause": "str?",
}  # fmt: skip
_ATTEMPT: dict[str, Kind] = {
    "run": "str", "phase": "str", "reason": "str", "ts": "str", "kind": "str", "patch_kept": "bool",
    "body_sha": "str", "cause": "str?", "cause_why": "str", "review": "object", "adversary": "object",
    "arbitration": "object", "fix_loop": "object", "evidence": "list", "malformed": "str",
}  # fmt: skip
_PHASE_RECORD: dict[str, Kind] = {
    "phase": "str", "status": "str", "parent": "str?", "parents": "list", "assembled_from": "list", "branch": "str",
    "base": "str", "land_at": "str", "reason": "str?", "rebased": "bool", "task_records": "list",
    "quarantined": "list", "batch": "list", "cleared_worktrees": "list", "reused_branch": "bool",
    "reused_tasks": "list", "recreated": "str", "refreshed": "str", "remote_push_failed": "str",
    "phase_branch_diverged": "list", "missing_branch": "str", "phase_failed_to_start": "str",
    "awaiting_tasks": "list", "base_rebuilt": "list", "stopped": "str", "interrupted": "list", "serialised": "json",
    "initiative_merge": "object", "phase_verdict": "object", "chunk_verdicts": "list", "level_checks": "list",
    "level_integration": "list", "level_integration_scope": "json", "trim": "str", "totals": "object",
    "manifest": "str", "manifest_record": "object",
}  # fmt: skip


def as_mapping(value: object) -> Mapping[str, object]:
    """`value` when it is a mapping, else an empty one: a cartridge or record section that is absent or null."""
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}


def as_list(value: object) -> list[object]:
    """`value` as a list when it is a list or a tuple, else an empty one."""
    return list(cast(Sequence[object], value)) if isinstance(value, list | tuple) else []


def as_json(value: object, where: str = "record") -> Json:
    """`value` as JSON: a scalar, or a list or a string-keyed dict of JSON, copied. Anything else is malformed."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Mapping):
        items = cast(Mapping[object, object], value)
        if not all(isinstance(key, str) for key in items):
            raise MalformedRecord(f"{where}: a key is not a string")
        return {str(key): as_json(item, f"{where}.{key}") for key, item in items.items()}
    if isinstance(value, list | tuple):
        return [as_json(item, f"{where}[]") for item in cast(Sequence[object], value)]
    raise MalformedRecord(f"{where}: {type(value).__name__} is not JSON")


def _is(kind: Kind, value: Json) -> bool:
    match kind:
        case "str":
            return isinstance(value, str)
        case "str?":
            return value is None or isinstance(value, str)
        case "int":
            return isinstance(value, int) and not isinstance(value, bool)
        case "int?":
            return value is None or (isinstance(value, int) and not isinstance(value, bool))
        case "bool":
            return isinstance(value, bool)
        case "object":
            return isinstance(value, dict)
        case "object?":
            return value is None or isinstance(value, dict)
        case "list":
            return isinstance(value, list)
        case "list?":
            return value is None or isinstance(value, list)
        case "json":
            return True


def _checked(raw: object, keys: Mapping[str, Kind], name: str, required: Sequence[str] = ()) -> JsonObject:
    """`raw` as a JSON object whose named keys hold their kind; `required` keys must be there."""
    record = as_json(raw, name)
    if not isinstance(record, dict):
        raise MalformedRecord(f"{name}: {type(raw).__name__} is not an object")
    missing = [key for key in required if key not in record]
    if missing:
        raise MalformedRecord(f"{name}: no {', '.join(missing)}")
    wrong = [f"{key} ({type(record[key]).__name__})" for key, kind in keys.items() if key in record and not _is(kind, record[key])]
    if wrong:
        raise MalformedRecord(f"{name}: the wrong type for {', '.join(wrong)}")
    return record


def task_record(raw: object) -> TaskRecord:
    """Pure. A run's task record from what a record file or `task_records.record_json` held."""
    return cast(TaskRecord, _checked(raw, _TASK_RECORD, "task record"))


def attempt_row(raw: Mapping[str, object]) -> AttemptRow:
    """Pure. One `attempts` row from its selected columns by name; `run_id`, `phase_id` and `cause` are always selected."""
    return cast(AttemptRow, _checked(raw, _ATTEMPT_ROW, "attempt row", ("run_id", "phase_id", "cause")))


def attempt(raw: object) -> Attempt:
    """Pure. One attempt from a work file's `attempts` entry, or as the driver rebuilt it from a run's task record."""
    return cast(Attempt, _checked(raw, _ATTEMPT, "attempt", ("run", "phase", "reason", "ts")))


def phase_record(raw: object) -> PhaseRecord:
    """Pure. A phase record: as a phase built it, or as the store's `phases` row holds it."""
    return cast(PhaseRecord, _checked(raw, _PHASE_RECORD, "phase record", ("phase", "status")))
