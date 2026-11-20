"""The row and record shapes the store modules share, as TypedDicts.

A row here is what a store module returns or builds: the keys are the columns (or, for a read, the decoded names),
so a caller sees every field without reading a query. Nothing in this module runs.
"""

from __future__ import annotations

from typing import TypedDict

from harness.store_dialect import Json, JsonObject

__all__ = [
    "Claim",
    "HostRow",
    "QueueRow",
    "StepRow",
    "TaskRecordWriteRow",
    "WorkItemRecord",
    "WorkItemRow",
    "WorkItemWriteRow",
]


class Claim(TypedDict):
    holder: str
    epoch: int
    expires_at: str


class WorkItemRecord(TypedDict):
    """A work item as a state write left it."""

    initiative: str
    task_id: str
    phase: str
    state: str
    needs: list[str]
    updated_at: str
    updated_by: str


class WorkItemRow(TypedDict):
    """One `work_items` row as stored; `needs_json` is the cell as the driver returns it."""

    initiative: str
    task_id: str
    phase: str | None
    state: str
    needs_json: Json
    updated_at: str | None
    updated_by: str | None


class QueueRow(TypedDict):
    """One `work_items` row with its queue and claim columns, the JSON columns decoded."""

    initiative: str
    task_id: str
    kind: str | None
    phase: str | None
    state: str
    needs: Json
    title: str | None
    surfaces: Json
    body: str | None
    extra: Json
    priority: int | None
    issue_number: int | None
    project_item_id: str | None
    parent_item: str | None
    updated_at: str | None
    updated_by: str | None
    holder: str | None
    epoch: int | None
    expires_at: str | None


class WorkItemWriteRow(TypedDict):
    """A `work_items` row as the write builder makes it: `needs_json` still a list, encoded when it is bound."""

    initiative: str
    task_id: str
    phase: str
    state: str
    needs_json: list[str]
    updated_at: str
    updated_by: str


class TaskRecordWriteRow(TypedDict):
    """A `task_records` row as the write builder makes it; `record_json` is encoded when it is bound."""

    run_id: str
    phase_id: str
    task_id: str
    record_json: JsonObject
    updated_at: str


class HostRow(TypedDict):
    """One `hosts` row, `versions_json` decoded."""

    name: str
    ssh: str
    capacity: int
    state: str
    beat_at: str
    versions_json: JsonObject
    updated_at: str
    updated_by: str


class StepRow(TypedDict):
    """One `step_results` row, written once and never changed; `result_json` and `artifacts_json` decoded."""

    run_id: str
    step: str
    initiative: str
    phase: str
    task_id: str
    kind: str
    round: int
    ok: int
    verdict: str
    subject_sha: str
    worker: str
    claim_epoch: int
    started_at: str
    finished_at: str
    cost_usd: float
    # The step's verdict, counts and check names. With artifacts on (`harness.artifacts`), a step whose artifacts were
    # not all written also carries `artifacts_error`: why, as one line. A row without the key is complete.
    result_json: JsonObject
    # Every object in storage the step wrote, each `{key, bytes, sha256, media}`, and `scrubbed: true` on one whose
    # bytes the secret scrub changed (never the patch, which is stored as written); nothing else.
    artifacts_json: list[JsonObject]

