"""The landing model's operations over the store and git together: start an initiative's record, merge a phase.

`merge_phase` is the one phase merge. A build run calls it the moment it finishes a phase, and the lander calls it
for every item left in the queue; both go through the same queue item, so at most one of them performs a given merge
and a worker that dies mid-merge is replaced once its claim lapses. The record is written only after git has moved.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from core import landing_record as lr
from harness import landing_git as lg
from harness import landing_store as ls
from harness.store_dialect import Connection

CLAIM_SECONDS = 900  # a phase merge with its level checks; a dead worker's claim is taken again after this
_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(_FORMAT)


def later(now: str, seconds: int) -> str:
    return stamp(datetime.strptime(now, _FORMAT).replace(tzinfo=UTC) + timedelta(seconds=seconds))


def phase_branch(initiative: str, phase: str) -> str:
    return f"epic/{initiative}/{phase}"


@dataclass(frozen=True)
class PhaseMerge:
    """What one `merge_phase` call did. `status`: merged, already_merged, conflicted, not_ready, busy, error."""

    status: str
    initiative: str
    phase: str
    tip: str = ""  # the phase tip that was merged
    merge_sha: str = ""  # the initiative branch tip afterwards
    paths: tuple[str, ...] = field(default_factory=tuple)
    detail: str = ""
    cause: str = ""  # for conflicted: `conflict`, `checks_failed` or `guard`
    merged_tasks: tuple[str, ...] = field(default_factory=tuple)
    assembled: bool = False  # every phase of the initiative is now merged

    @property
    def ok(self) -> bool:
        return self.status in ("merged", "already_merged")


def start_record(conn: Connection, repo: Path | str, *, initiative: str, branch: str, parent: str, parent_ref: str,
                 now: str, land_at: str = "initiative", epic: str | None = None, stack_base: str | None = None,
                 adopted: bool = False) -> tuple[dict[str, Any] | None, str]:
    """The initiative's landing record, made on first use: cut the branch from the parent and record where.

    A record that exists is returned as it is: the parent no longer floats, and the branch is never cut twice.
    (record, why not)."""
    found = ls.get_record(conn, initiative)
    if found is not None:
        tip, _, why = lg.ensure_branch(repo, str(found["branch"]), str(found.get("base_sha") or parent_ref))
        return (found, "") if tip else (None, why)
    tip, created, why = lg.ensure_branch(repo, branch, parent_ref)
    if tip is None:
        return None, why
    base = tip if created else (_merge_base(repo, branch, parent_ref) or tip)
    record = lr.new_record(initiative, repo=str(repo), branch=branch, parent=parent, base_sha=base, now=now,
                           land_at=land_at, epic=epic, stack_base=stack_base, adopted=adopted)
    ls.put_record(conn, record)
    return record, ""


def _merge_base(repo: Path | str, branch: str, parent_ref: str) -> str | None:
    ok, out = lg.git(repo, "merge-base", branch, parent_ref)
    return out if ok and out else None


def queue_phase(conn: Connection, record: Mapping[str, Any], phase: str, *, now: str, reason: str) -> bool:
    """Put a built phase in the land queue and mark it `queued` in the record. True when this call queued it."""
    queued = ls.enqueue(conn, ls.PHASE_MERGE, str(record["initiative"]), phase=phase, repo=str(record.get("repo") or ""),
                        now=now, reason=reason)
    stored = str(((record.get("phases") or {}).get(phase) or {}).get("state") or "open")
    if stored != "merged" and stored != "queued" and lr.phase_move_allowed(stored, "queued"):
        ls.put_record(conn, lr.with_phase(record, phase, now, state="queued"))
    return queued


def _green_waves(record: Mapping[str, Any], tasks: Sequence[Mapping[str, Any]], tip: str) -> dict[str, Any]:
    """The record's `waves` with the tip stamped on every wave that has just gone green."""
    stored = dict(record.get("waves") or {})
    for row in lr.wave_states(record, tasks):
        key = str(row["wave"])
        if row["state"] == "green" and not (stored.get(key) or {}).get("green_tip"):
            stored[key] = {"green_tip": tip, "phases": row["phases"]}
    return stored


def merge_phase(conn: Connection, repo: Path | str, *, initiative: str, phase: str, tasks: Sequence[Mapping[str, Any]],
                worker: str, now: str, checks: lg.Checks | None = None, source: str | None = None,
                claim_seconds: int = CLAIM_SECONDS) -> PhaseMerge:
    """Merge one built phase into its initiative branch, under the phase's queue item.

    `tasks` are the initiative's task rows (`id`, `phase`, `state`, `needs`). The phase must be built: every task
    approved, done or dropped, and every parent phase merged. The merge refreshes and merges in one commit on the
    initiative tip, runs `checks` on the merged tree, and moves the branch only if they pass and no recorded sha is
    dropped. A conflict or a red check marks the phase `conflicted` and fails the item with the reason; the caller
    files the rectify task. Calling again for a merged phase changes nothing."""
    record = ls.get_record(conn, initiative)
    if record is None:
        return PhaseMerge("error", initiative, phase, detail=f"{initiative} has no landing record")
    state = lr.phase_state(record, phase, tasks)
    if state == "merged":
        entry = (record.get("phases") or {}).get(phase) or {}
        return PhaseMerge("already_merged", initiative, phase, tip=str(entry.get("tip") or ""),
                          merge_sha=str(entry.get("merge_sha") or ""),
                          assembled=lr.initiative_state(record, tasks) in ("assembled", "pr_open", "landed"))
    if state not in ("built", "queued"):
        return PhaseMerge("not_ready", initiative, phase, detail=f"phase {phase} is {state}, not built")
    ident = ls.item_id(ls.PHASE_MERGE, initiative, phase)
    ls.enqueue(conn, ls.PHASE_MERGE, initiative, phase=phase, repo=str(record.get("repo") or repo), now=now, reason="phase built")
    if not ls.claim(conn, ident, worker, now=now, lease_until=later(now, claim_seconds)):
        holder = (ls.get_item(conn, ident) or {}).get("claimed_by") or "another worker"
        return PhaseMerge("busy", initiative, phase, detail=f"{holder} is merging phase {phase}")
    branch = str(record["branch"])
    merged = lg.merge_into(repo, branch=branch, source=source or phase_branch(initiative, phase),
                           message=f"{initiative}: merge phase {phase}", recorded=lr.recorded_shas(record), checks=checks)
    # Read again: the record may have moved (a sibling phase merged) while git worked. Only this phase's entry is ours.
    record = ls.get_record(conn, initiative) or record
    if merged.ok:
        tip = merged.tip
        updated = lr.with_phase(record, phase, now, state="merged", tip=merged.merged, merge_sha=tip, error="")
        updated = {**updated, "waves": _green_waves(updated, tasks, tip)}
        assembled = lr.initiative_state(updated, tasks) == "assembled"
        if assembled and updated.get("state") in ("building", "conflicted", "blocked"):
            updated = lr.with_state(updated, "assembled", now)
        ls.put_record(conn, updated)
        ls.finish(conn, ident, worker, now=now)
        done = tuple(str(t["id"]) for t in lr.phase_tasks(tasks, phase) if t.get("state") == "approved")
        return PhaseMerge("merged", initiative, phase, tip=merged.merged, merge_sha=tip, merged_tasks=done, assembled=assembled)
    if merged.status == "error":
        ls.fail(conn, ident, worker, now=now, error=merged.detail)
        return PhaseMerge("error", initiative, phase, detail=merged.detail)
    cause = {"conflict": "conflict", "checks_failed": "checks_failed", "guard": "guard"}[merged.status]
    updated = lr.with_phase(record, phase, now, state="conflicted", error=merged.detail, conflict_paths=list(merged.paths),
                            cause=cause)
    ls.put_record(conn, updated)
    ls.fail(conn, ident, worker, now=now, error=merged.detail)
    return PhaseMerge("conflicted", initiative, phase, tip=merged.merged, paths=merged.paths, detail=merged.detail, cause=cause)
