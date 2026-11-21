"""The landing model's operations over the store and git together: start an initiative's record, merge a phase.

`merge_phase` is the one phase merge. A build run calls it the moment it finishes a phase, and the lander calls it
for every item left in the queue; both go through the same queue item, so at most one of them performs a given merge
and a worker that dies mid-merge is replaced once its claim lapses. The record is written only after git has moved.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from core import landing_record as lr
from harness import landing_git as lg
from harness import landing_store as ls
from harness import lease_keeper, store_lease
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
    resolved_by: str = ""  # who resolved the merge's conflict (`resolve`, the new model only); "" for a clean merge
    unsettled: str = ""  # a conflict `resolve` was asked about and did not settle: why (`lg.Unsettled`)

    @property
    def ok(self) -> bool:
        return self.status in ("merged", "already_merged")


def start_record(conn: Connection, repo: Path | str, *, initiative: str, branch: str, parent: str, parent_ref: str,
                 now: str, land_at: str = "initiative", epic: str | None = None, stack_base: str | None = None,
                 adopted: bool = False, lane_model: str | None = None) -> tuple[lr.LandingRecord | None, str]:
    """The initiative's landing record, made on first use: cut the branch from the parent and record where.

    A record that exists is returned as it is: the parent no longer floats, the branch is never cut twice, and its
    `lane_model` (the initiative's, stamped at creation) never changes. (record, why not)."""
    found = ls.get_record(conn, initiative)
    if found is not None:
        tip, _, why = lg.ensure_branch(repo, str(found["branch"]), str(found.get("base_sha") or parent_ref))
        return (found, "") if tip else (None, why)
    tip, created, why = lg.ensure_branch(repo, branch, parent_ref)
    if tip is None:
        return None, why
    base = tip if created else (_merge_base(repo, branch, parent_ref) or tip)
    record = lr.new_record(initiative, repo=str(repo), branch=branch, parent=parent, base_sha=base, now=now,
                           land_at=land_at, epic=epic, stack_base=stack_base, adopted=adopted, lane_model=lane_model)
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


def branch_lease(initiative: str) -> str:
    """The lease every move of one initiative's branch is made under, whichever host or worker makes it."""
    return f"initiative-branch:{initiative}"


def take_parent_forward(conn: Connection, repo: Path | str, *, initiative: str, parent_ref: str, worker: str, now: str,
                        fetch: Callable[[str], str | None] | None = None,
                        publish: Callable[[str], str | None] | None = None,
                        lease_seconds: int = CLAIM_SECONDS) -> lg.Merge:
    """Merge the initiative's parent, as it stands at `parent_ref`, into the initiative branch. Always forward.

    Under the branch lease, after `fetch`, with the guard: a merge that would drop a recorded sha is refused. A
    conflict leaves the branch where it was and names the paths; resolving it is build work (the `integrate` phase),
    never this function's. `up_to_date` when the branch already holds the parent; `busy` when another worker holds the
    branch lease."""
    record = ls.get_record(conn, initiative)
    if record is None:
        return lg.Merge("error", detail=f"{initiative} has no landing record")
    branch = str(record["branch"])
    held = store_lease.acquire(conn, branch_lease(initiative), worker, now, lease_seconds)
    if not held.ok:
        return lg.Merge("busy", detail=f"{held.holder or 'another worker'} is moving the branch of {initiative}")
    try:
        if fetch is not None and (refused := fetch(branch)):
            return lg.Merge("error", detail=refused)
        merged = lg.merge_into(repo, branch=branch, source=parent_ref, recorded=lr.recorded_shas(record),
                               message=f"{initiative}: merge {record.get('parent')} into {branch}")
        if merged.status == "merged" and publish is not None and (warning := publish(branch)):
            return lg.Merge("merged", tip=merged.tip, merged=merged.merged, detail=warning)
        return merged
    finally:
        store_lease.release(conn, branch_lease(initiative), worker, int(held.epoch or 0))


def off_branch(repo: Path | str, source: str, tasks: Sequence[Mapping[str, Any]], phase: str) -> list[str]:
    """The phase's approved tasks whose recorded `merged_commit` the phase tip does not hold: merging the phase now
    would leave their work behind. A task with no recorded commit is not judged here."""
    return [
        str(t["id"]) for t in lr.phase_tasks(tasks, phase)
        if t.get("state") == "approved" and t.get("merged_commit") and not lg.is_ancestor(repo, str(t["merged_commit"]), source)
    ]


def after_push(merge_status: str, push_warning: str | None) -> str:
    """Pure. What a phase merge git has made does once the push of the initiative branch is answered: `record` it (the
    push was taken), or, refused, `unwind` the merge this call made (the local branch goes back to origin's tip, so the
    next pass merges again on what origin holds) or `fail` it as it is (the branch already held the phase: nothing to
    undo). Either way nothing is recorded: a phase recorded merged is always on origin."""
    if push_warning is None:
        return "record"
    return "unwind" if merge_status == "merged" else "fail"


def merge_phase(conn: Connection, repo: Path | str, *, initiative: str, phase: str, tasks: Sequence[Mapping[str, Any]],
                worker: str, now: str, checks: lg.Checks | None = None, source: str | None = None,
                claim_seconds: int = CLAIM_SECONDS, fetch: Callable[[str], str | None] | None = None,
                publish: Callable[[str], str | None] | None = None, resolve: lg.Resolve | None = None,
                clock: Callable[[], str] | None = None, beat_s: float | None = None) -> PhaseMerge:
    """Merge one built phase into its initiative branch, under the phase's queue item.

    `tasks` are the initiative's task rows (`id`, `phase`, `state`, `needs`). The phase must be built: every task
    approved, done or dropped, and every parent phase merged. The merge refreshes and merges in one commit on the
    initiative tip, runs `checks` on the merged tree, and moves the branch only if they pass and no recorded sha is
    dropped. A conflict or a red check marks the phase `conflicted` and fails the item with the reason; the caller
    files the rectify task. Calling again for a merged phase changes nothing.

    The initiative branch has one mover at a time, on any host: the merge runs under `branch_lease`. `fetch` brings
    the branch up to origin's copy first (it returns why the merge must not go ahead here, or None), and `publish`
    pushes the merged branch (it returns a warning, or None). Both are given the branch name. With `publish`, the
    phase is recorded merged only once the push is taken (`after_push`): a refused push unwinds the merge and fails
    the item, which the sweep queues again.

    `resolve` (the new model's merge agent, `lg.merge_into`) is given a conflict before it is one: the branch moves only
    if what it answers passes the same guard and `checks`, and the phase entry says who resolved it (`resolved_by`).
    Without it a conflict is a conflict, as it always was.

    The merge agent and the gate together can outlive `claim_seconds`, so the branch lease is renewed on a thread
    (`lease_keeper.Keeper`, every `beat_s`, by `clock`) while the merge runs, and once more just before the merge is
    pushed (or, with no `publish`, recorded): a renew the store refuses means another writer may hold the branch, so
    the merge this call made is unwound and the item fails with nothing pushed. Without `clock` (the old model, and
    the build run's own merge) there is no keeper: the lease is held for `claim_seconds` from `now`, as it always
    was, and nothing is renewed or confirmed."""
    held = store_lease.acquire(conn, branch_lease(initiative), worker, now, claim_seconds)
    if not held.ok:
        return PhaseMerge("busy", initiative, phase, detail=f"{held.holder or 'another worker'} is moving the branch of {initiative}")
    keeper = None if clock is None else lease_keeper.Keeper(
        conn, branch_lease(initiative), worker, int(held.epoch or 0), ttl=claim_seconds, clock=clock, interval=beat_s,
    ).start()
    try:
        return _merge_phase(conn, repo, initiative=initiative, phase=phase, tasks=tasks, worker=worker, now=now,
                            checks=checks, source=source, claim_seconds=claim_seconds, fetch=fetch, publish=publish,
                            resolve=resolve, keep=keeper)
    finally:
        if keeper is not None:
            keeper.stop()
        store_lease.release(conn, branch_lease(initiative), worker, int(held.epoch or 0))


def _merge_phase(conn: Connection, repo: Path | str, *, initiative: str, phase: str, tasks: Sequence[Mapping[str, Any]],
                 worker: str, now: str, checks: lg.Checks | None, source: str | None, claim_seconds: int,
                 fetch: Callable[[str], str | None] | None, publish: Callable[[str], str | None] | None,
                 resolve: lg.Resolve | None = None, keep: lease_keeper.Keeper | None = None) -> PhaseMerge:
    record = ls.get_record(conn, initiative)
    if record is None:
        return PhaseMerge("error", initiative, phase, detail=f"{initiative} has no landing record")
    if lr.holds_merges(record.get("held")):
        # The lander holds an initiative whose record and origin's branch disagree: nothing merges into it until they
        # agree. A hold of the pull request alone (D2's review hold) lets the work that answers it merge.
        why = (record.get("held") or {}).get("reason") or "its record and its branch disagree"
        return PhaseMerge("not_ready", initiative, phase, detail=f"{initiative} is held: {why}")
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
    tip_ref = source or phase_branch(initiative, phase)
    if lg.rev(repo, tip_ref) is not None and (loose := off_branch(repo, tip_ref, tasks, phase)):
        # Left queued: the phase is not whole in this clone. Whoever holds the commits (or the carry rule) merges it.
        return PhaseMerge("not_ready", initiative, phase,
                          detail=f"approved task(s) {', '.join(loose)} are not on {tip_ref} in this clone")
    if fetch is not None and (refused := fetch(str(record["branch"]))):
        return PhaseMerge("error", initiative, phase, detail=refused)
    if not ls.claim(conn, ident, worker, now=now, lease_until=later(now, claim_seconds)):
        holder = (ls.get_item(conn, ident) or {}).get("claimed_by") or "another worker"
        return PhaseMerge("busy", initiative, phase, detail=f"{holder} is merging phase {phase}")
    branch = str(record["branch"])
    before = lg.rev(repo, f"refs/heads/{branch}")
    merged = lg.merge_into(repo, branch=branch, source=source or phase_branch(initiative, phase),
                           message=f"{initiative}: merge phase {phase}", recorded=lr.recorded_shas(record), checks=checks,
                           resolve=resolve)
    if merged.ok and keep is not None and not keep.confirm():
        # The branch lease lapsed or was taken while the merge ran: another writer may move the branch now, so this
        # merge is neither pushed nor recorded. The local branch goes back to where it was; the sweep queues it again.
        if merged.status == "merged" and before:
            lg.git(repo, "update-ref", "-m", "lander: the branch lease was lost", f"refs/heads/{branch}", before, merged.tip)
        detail = f"{keep.why or 'the branch lease could not be renewed'}; phase {phase} was not pushed or recorded merged"
        ls.fail(conn, ident, worker, now=now, error=detail)
        return PhaseMerge("error", initiative, phase, tip=merged.merged, detail=detail)
    if merged.ok and publish is not None:
        # Pushed before it is recorded: a phase recorded merged is on origin, where every host reads it.
        warning = publish(branch)
        step = after_push(merged.status, warning)
        if step != "record":
            if step == "unwind" and before:
                lg.git(repo, "update-ref", "-m", "lander: origin refused the merge", f"refs/heads/{branch}", before, merged.tip)
            detail = f"{warning}; phase {phase} is not recorded merged until origin holds its merge"
            ls.fail(conn, ident, worker, now=now, error=detail)
            return PhaseMerge("error", initiative, phase, tip=merged.merged, detail=detail)
    # Read again: the record may have moved (a sibling phase merged) while git worked. Only this phase's entry is ours.
    record = ls.get_record(conn, initiative) or record
    if merged.ok:
        tip = merged.tip
        carried = [str(t["id"]) for t in lr.phase_tasks(tasks, phase) if t.get("state") == "approved"]
        updated = lr.with_phase(record, phase, now, state="merged", tip=merged.merged, merge_sha=tip, error="", tasks=carried)
        if merged.resolved_by:
            updated = lr.with_phase(updated, phase, now, resolved_by=merged.resolved_by)
        updated = {**updated, "waves": _green_waves(updated, tasks, tip)}
        assembled = lr.initiative_state(updated, tasks) == "assembled"
        if assembled and updated.get("state") in ("building", "conflicted", "blocked"):
            updated = lr.with_state(updated, "assembled", now)
        ls.put_record(conn, updated)
        ls.finish(conn, ident, worker, now=now)
        done = tuple(str(t["id"]) for t in lr.phase_tasks(tasks, phase) if t.get("state") == "approved")
        return PhaseMerge("merged", initiative, phase, tip=merged.merged, merge_sha=tip, merged_tasks=done,
                          assembled=assembled, resolved_by=merged.resolved_by)
    if merged.status == "error":
        ls.fail(conn, ident, worker, now=now, error=merged.detail)
        return PhaseMerge("error", initiative, phase, detail=merged.detail)
    cause = {"conflict": "conflict", "checks_failed": "checks_failed", "guard": "guard"}[merged.status]
    updated = lr.with_phase(record, phase, now, state="conflicted", error=merged.detail, conflict_paths=list(merged.paths),
                            cause=cause)
    ls.put_record(conn, updated)
    ls.fail(conn, ident, worker, now=now, error=merged.detail)
    return PhaseMerge("conflicted", initiative, phase, tip=merged.merged, paths=merged.paths, detail=merged.detail, cause=cause,
                      unsettled=merged.unsettled)


# -- rectify tasks: what the lander and the epic file when a merge conflicts or fails its checks -----------------


RECTIFY_OPEN_STATES = frozenset({"todo", "ready", "in_progress", "blocked", "quarantined"})


def rectify_task(phase: str, tasks: Sequence[Mapping[str, Any]]) -> tuple[str, bool]:
    """Pure. (the rectify task's id for `phase`, whether it is one already open).

    A phase has at most one open rectify task: a merge that fails again before it is built names the same task.
    A new one is `<phase>-rectify`, then `-2`, `-3`: the first id no task of the initiative has."""
    stem = f"{phase}-rectify"
    mine = [t for t in tasks if str(t.get("phase")) == phase and (str(t["id"]) == stem or str(t["id"]).startswith(f"{stem}-"))]
    still_open = [str(t["id"]) for t in mine if str(t.get("state")) in RECTIFY_OPEN_STATES]
    if still_open:
        return sorted(still_open)[-1], True
    taken = {str(t["id"]) for t in tasks}
    number = 1
    while (ident := stem if number == 1 else f"{stem}-{number}") in taken:
        number += 1
    return ident, False


def rectify_body(branch: str, phase: str, paths: Sequence[str], check_output: str) -> str:
    """The body of a rectify task. It names what to fix and says, first, that nothing already built is to be redone."""
    keep = (
        f"The work of phase `{phase}` is already built, reviewed and on this phase's branch, and the tip of `{branch}` "
        "has been merged into it. Start from this tree. Change only what the problem below needs: do not rebuild, "
        "rewrite or remove work that is already here."
    )
    if paths:
        listed = "\n".join(f"- `{p}`" for p in paths)
        return (
            f"{keep}\n\n**Problem**\n\nMerging `{branch}` into this phase conflicted. The files below hold conflict markers "
            f"(`<<<<<<<`, `=======`, `>>>>>>>`), with both sides present:\n\n{listed}\n\n**Wanted**\n\nResolve each conflict so "
            "both sides' intent holds, remove every marker, and keep the checks passing.\n\n**Checks**\n\nNo conflict marker "
            "is left in the tree, and the phase's checks pass."
        )
    return (
        f"{keep}\n\n**Problem**\n\nThis phase merges cleanly with `{branch}`, but the checks fail on the merged tree:\n\n"
        f"```\n{check_output.strip()[:4000]}\n```\n\n**Wanted**\n\nFix what the merge broke, so the checks pass on this tree.\n\n"
        "**Checks**\n\nThe checks above pass."
    )
