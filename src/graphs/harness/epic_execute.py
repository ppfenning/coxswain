"""The epic driver, executing what was cleared: merges, records, and the rescue of a quarantined task."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from core import ledger, workstore
from core.jsontypes import is_list
from core.manifest import append_ledger, build_manifest, gate_diff
from graphs._contract import proposal
from graphs.delivery import rescue_review
from harness import rescue_checks, rescue_select
from harness.autonomy import split_by_policy
from harness.checks import HARNESS_FAULT_PREFIX
from harness.epic_batch import draft_note, item_path, quarantine_task, state_move_apply, unkept_work
from harness.epic_branches import (
    IDENTITY,
    checks_env,
    fetch_phase_branches,
    open_review_pr,
    opens_pr,
    push_review_branch,
    run_scratch,
)
from harness.epic_build import body_stamp
from harness.epic_context import Ctx, git_in
from harness.epic_phase_branch import branch_exists, locked_merges, publish_branch, rebase_phase_branch
from harness.epic_records import (
    END_WRITE_WINDOW_S,
    Execution,
    ctx_store,
    epic_log,
    fence_reason,
    mirror_by,
    mirror_write,
    record_steps,
    record_tasks,
    refuse_stale,
    repair_file_state,
    save_task_result,
    store_authoritative,
    store_first,
    utc_now,
    wait_if_paused_here,
)
from harness.epic_types import (
    Attempt,
    AttemptRow,
    EpicItem,
    MalformedRecord,
    TaskRecord,
    as_list,
    as_mapping,
    attempt_row,
    task_record,
)
from harness.epic_types import attempt as as_attempt
from harness.gate import apply_arm_for, auto_apply, gate
from harness.handoff import build_handoff
from harness.phase_remote import Kind, missing_branch_cause
from harness.resume import load_result
from harness.store_write import record_handoff, record_merge
from harness.worktree import apply_patch, create_worktree, link_venv, remove_worktree
from runner.protocol import LimitStop, RunnerError


def without_cause(saved: Mapping[str, object]) -> dict[str, object]:
    """A saved result as a later run reuses it. Its cause, and the body it was quarantined on, name the run that
    quarantined it, not this one."""
    return {k: v for k, v in saved.items() if k not in ("cause", "cause_why", "body_sha")}


def execute_cleared(
    ctx: Ctx,
    item: Mapping[str, object],
    *,
    slot: str,
    subject: str,
    phase: str,
    state: Execution,
    by_id: Mapping[str, EpicItem],
) -> tuple[bool, str]:
    """Do what the gate — or the policy — cleared. Dispatch on the KIND, not the slot.

    The kind is what governs, and escalation rewrites it: a `draft_pr_create`
    whose patch touched governance arrives here as `self_modification`, falls
    through to the arm the cartridge names (`pr`, which has no executor here),
    and reports honestly that nothing happened. That is what makes an escalated
    task's merge impossible to earn rather than merely discouraged.
    """
    stale = fence_reason(ctx)
    if stale is not None:
        refuse_stale(state, phase=phase, subject=subject, slot=slot, reason=stale)
        return False, stale
    wait_if_paused_here(ctx)
    stale = fence_reason(ctx)  # the wait can outlast this driver's lease
    if stale is not None:
        refuse_stale(state, phase=phase, subject=subject, slot=slot, reason=stale)
        return False, stale

    kind = item.get("kind")

    if kind == "draft_pr_create" and slot == "draft":
        # Landing the draft IS creating the branch. There is no forge arm here,
        # so the "draft PR" is a local branch until one exists — and a branch
        # nobody has opened has exactly the blast radius the taxonomy prices it
        # at: the cost of a wrong one is a branch nobody reads.
        draft = ctx.draft_branch(phase, subject)
        ok, detail = git_in("-C", str(ctx.repo), "branch", draft, ctx.scratch_branch(subject))
        state.landed[subject] = ok
        if not ok:
            state.draft_detail[subject] = f"draft branch {draft} was not created ({detail})"
        else:
            state.push_warnings.extend(filter(None, [publish_branch(ctx, draft)]))
        return ok, detail or f"created {draft}"

    if kind == "merge_stack" and slot == "merge":
        # Task branch -> its parent PHASE branch, and nothing else. No path in
        # this driver merges to a default branch: `merge_main` is never emitted,
        # never executed, and unreachable from here at any comfort level and on
        # any streak. The swarm's output is branches and drafts.
        if not state.landed.get(subject):
            return False, "the draft did not land, so its merge is refused with it"
        draft = ctx.draft_branch(phase, subject)
        if not branch_exists(ctx, draft):
            # A draft another host created and pushed: make it local, or refuse with the cause.
            found = fetch_phase_branches(ctx, [draft])[draft]
            if found.kind in (Kind.MISSING_EVERYWHERE, Kind.FETCH_FAILED):
                return False, missing_branch_cause(draft, found)
        ok, detail = git_in(
            *IDENTITY, "-C", str(ctx.phase_worktree(phase)), "merge", "--no-ff", draft,
            "-m", f"epic {ctx.run_id}: merge {subject} into {phase}",
        )
        if not ok:
            git_in("-C", str(ctx.phase_worktree(phase)), "merge", "--abort")
            state.quarantined.append(
                quarantine_task(
                    ctx,
                    by_id,
                    phase=phase,
                    task=subject,
                    reason=f"merge conflict: {detail}",
                    kind="infra",
                    cause=("harness", "merge conflict"),
                )
            )
        state.merged[subject] = ok
        if ok:
            head_ok, head = git_in("-C", str(ctx.phase_worktree(phase)), "rev-parse", "HEAD")
            if head_ok:
                state.commits[subject] = head
            state.push_warnings.extend(filter(None, [publish_branch(ctx, ctx.phase_branch(phase))]))
        return ok, detail

    if kind == "stack_rebase" and slot == "rebase":
        ok, detail = rebase_phase_branch(ctx, phase, _rebase_base(item), locked_merges(ctx, by_id.values(), phase))
        if not ok:
            state.quarantined.append(
                {"id": phase, "phase": phase, "grain": "phase", "reason": f"rebase conflict: {detail}"}
            )
        return ok, detail

    if kind == "self_modification" and slot == "draft" and item.get("escalated_from") == "draft_pr_create":
        # A review PR a human reads, never a merge: no merge call runs here. It is a task PR, so it follows land_at.
        if not opens_pr(ctx.land_at, "task"):
            return False, f"escalated: its review PR opens only at land_at task, not {ctx.land_at}"
        ok, detail = push_review_branch(ctx, subject)
        if not ok:
            return False, f"review branch push failed: {detail}"
        paths = next(
            (
                str(row.get("output") or "")
                for row in map(as_mapping, as_list(item.get("evidence")))
                if row.get("check") == "governance_paths"
            ),
            "",
        )
        body = "\n".join(
            (
                f"Review verdict: {item.get('review_verdict') or 'none'}",
                f"Arbiter verdict: {item.get('arbitration_verdict') or 'none'}",
                f"Governance paths: {paths or 'none'}",
            )
        )
        ok, detail = open_review_pr(
            ctx,
            title=f"[review] {ctx.initiative_id}: {by_id[subject]['title']}",
            body=body,
            head=f"review/{ctx.initiative_id}/{subject}",
            # `gh pr create --base` takes a branch on the remote, not the `origin/` ref the phase branches are cut from.
            base=ctx.default_ref.removeprefix("origin/"),
        )
        if not ok:
            return False, f"review PR failed: {detail}"
        state.review_pr[subject] = detail
        return True, detail

    # Everything else goes to the arm the cartridge names — the same call
    # `gate.apply_decisions` makes, because an apply arm is a role and the same
    # runner that ran the read-only nodes runs the write. The build itself was
    # already reviewed, adversaried and arbitrated; a `RunnerError` here is the
    # arm's own infrastructure failing, not the task, so it quarantines the
    # task as `infra` and lets the phase continue rather than crashing `run_epic`.
    if slot == "state_move":
        # Before any store or apply-arm write: `approved` is only true of work on a branch that survives the run.
        why = unkept_work(ctx, phase, subject, state)
        # A draft that did not land is not lost work: the task branch holds it and `runs land` opens the PR.
        # On a lane host that branch is in the host's repo, so it counts there and is fetched to the chair later.
        note = draft_note(ctx, subject, state) if why is not None else None
        if note is not None:
            state.notes[subject] = note
            why = None
        if why is not None:
            state.moved[subject] = False
            if not any(q.get("id") == subject and q.get("grain") == "task" for q in state.quarantined):
                state.quarantined.append(
                    quarantine_task(
                        ctx,
                        by_id,
                        phase=phase,
                        task=subject,
                        reason=f"work not on a surviving branch: {why}",
                        kind="infra",
                        cause=("harness", "work not on a surviving branch"),
                    )
                )
            return False, why
    if slot == "state_move" and store_authoritative(ctx) and by_id.get(subject) is not None:
        # The store's acceptance is the move. The file is a cache, so no arm runs; the driver writes its line.
        refused = store_first(ctx, by_id[subject], "approved")
        if refused is not None:
            state.moved[subject] = False
            return False, refused
        if not repair_file_state(by_id[subject], "approved"):
            epic_log.warning("store moved %s to approved; file state line not written, regenerate-states repairs it", subject)
        state.moved[subject] = True
        _record_merge(ctx, state, subject)
        return True, "store moved to approved; file state line written"
    try:
        applied, detail = auto_apply(
            dict(item), cartridge=dict(ctx.cartridge), runner=ctx.runner, wait_if_paused=lambda: wait_if_paused_here(ctx)
        )
    except LimitStop:
        raise  # the account's own limit, not this task's; never through `quarantine_task`
    except RunnerError as exc:
        arm = apply_arm_for(cast(str, item.get("kind")), dict(ctx.cartridge))
        reason = f"apply arm '{arm}' raised {type(exc).__name__}: {exc}"
        state.quarantined.append(quarantine_task(ctx, by_id, phase=phase, task=subject, reason=reason, kind="infra"))
        return False, reason
    if slot == "state_move":
        state.moved[subject] = applied
        if applied:
            mirror_write(ctx, by_id.get(subject), "approved")
    return applied, detail


def _record_merge(ctx: Ctx, state: Execution, task: str) -> None:
    """work_state: store. Stamp the merge commit and the handoff on the task's approved row. Errors are logged, never raised.

    Runs after the move to approved, because `record_merge` refuses any other state.
    """
    commit = state.commits.get(task)
    if commit is None or not state.merged.get(task):
        return
    try:
        handoff = build_handoff(state.builds.get(task) or {}, ctx.scratch_branch(task), commit)
        record_merge(ctx_store(ctx).conn, ctx.initiative_id, task, commit, utc_now(), mirror_by(ctx))
        record_handoff(ctx_store(ctx).conn, ctx.initiative_id, task, handoff, utc_now(), mirror_by(ctx))
    except Exception as exc:
        epic_log.warning("merge and handoff not written to the store row: task=%s: %s: %s", task, type(exc).__name__, exc)
        return
    state.handoffs[task] = handoff


def _rebase_base(item: Mapping[str, object]) -> str:
    """The ref a `stack_rebase` proposal names as the ground that moved.

    Read back off the proposal rather than recomputed, so what executes is the
    thing the gate was shown: a rebase onto a ref nobody approved is a different
    write from the one that was decided.
    """
    return str(item.get("suggested_action") or "").rsplit(" onto ", 1)[-1].strip()


# ── rescue: a kept patch the harness, not the builder, failed to land ────────


RESCUE_PRINCIPAL = "epic-swarm(rescue-review)"


def _rescue_failed(ctx: Ctx, item: Mapping[str, object], *, phase: str, reason: str, failed_checks: bool) -> dict[str, object]:
    """The task stays quarantined: a `rescue_failed` attempt carrying the rule's cause, and a quarantined task row."""
    task = str(item["id"])
    cause, cause_why = rescue_select.rescue_cause(failed_checks, reason)
    entry = quarantine_task(
        ctx, {task: dict(item)}, phase=phase, task=task, reason=reason, kind="rescue_failed", cause=(cause, cause_why)
    )
    record_tasks(ctx, phase, [], [entry])
    return {"status": "rescue_failed", "reason": reason, "cause": cause}


def records_after[A: Mapping[str, object], R](
    found: Sequence[tuple[A, R]], cutoff: Mapping[str, int]
) -> list[tuple[A, R]]:
    """Pure. The (attempt, record) pairs whose run counts after the fresh-start `cutoff` (`workstore.counts_after`)."""
    return [(attempt, record) for attempt, record in found if workstore.counts_after(attempt.get("run"), cutoff)]


def with_record_attempts(item: EpicItem, recorded: Sequence[Attempt]) -> EpicItem:
    """Pure. `item` with `recorded` (its runs' task records as attempts, oldest first) as its attempts, when it holds
    none of its own; an item whose work file holds attempts, or with nothing recorded, is returned as it is."""
    return {**item, "attempts": [a.copy() for a in recorded]} if recorded and not item.get("attempts") else item


def _seq(row: Mapping[str, object]) -> int:
    """The `seq` of an attempts row that selected it."""
    seq = row["seq"]
    if not isinstance(seq, int):
        raise MalformedRecord(f"attempt row: seq is a {type(seq).__name__}, not a number")
    return seq


def with_stored_causes(item: Mapping[str, object], rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """`item` with each causeless file attempt given the cause of the store row for its run and phase, newest seq."""
    newest = {(r["run_id"], r["phase_id"]): r["cause"] for r in sorted(rows, key=_seq)}
    return {
        **item,
        "attempts": [
            {**a, "cause": newest[key]} if not a.get("cause") and newest.get(key) else a
            for a in map(as_mapping, as_list(item["attempts"]))
            for key in [(a.get("run"), a.get("phase"))]
        ],
    }


def with_stored_rescues(item: Mapping[str, object], rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """`item` with the store's `rescue_failed` attempts folded into its own, in ts order.

    core's ATTEMPT_KINDS has no `rescue_failed`, so the work file never holds one and the store row is the
    record. A row no older than the first attempt on the current body is stamped with that body's sha.
    """
    current = workstore.body_sha(str(item["body"]))
    attempts = [as_mapping(a) for a in as_list(item["attempts"])]
    since = min((str(a.get("ts") or "") for a in attempts if a.get("body_sha", current) == current), default=None)
    stored = [
        {
            "run": row["run_id"],
            "phase": row["phase_id"],
            "kind": row["kind"],
            "reason": row["reason"],
            "ts": row["ts"],
            "cause": row["cause"],
            "body_sha": current if since is not None and str(row["ts"]) >= since else "before the current body",
        }
        for row in rows
        if row["kind"] == rescue_select.RESCUE_KIND
    ]
    return {**item, "attempts": sorted([*attempts, *stored], key=lambda a: str(a.get("ts") or ""))}


def _stored_rescues(ctx: Ctx, task: str) -> list[AttemptRow]:
    """The store's `rescue_failed` attempts for `task`, any run. Empty without a store."""
    if ctx.store is None:
        return []
    cols = ("run_id", "phase_id", "kind", "reason", "ts", "cause")
    mark = ctx.store.conn.dialect.placeholder
    sql = f"SELECT {', '.join(cols)} FROM attempts WHERE task_id = {mark} AND kind = {mark}"
    return _attempt_rows(cols, ctx.store.conn.query_all(sql, (task, rescue_select.RESCUE_KIND)))


def stored_causes(ctx: Ctx, task: str) -> list[AttemptRow]:
    """The store's attempt causes for `task`, any run and kind. Empty without a store."""
    if ctx.store is None:
        return []
    cols = ("run_id", "phase_id", "seq", "cause")
    mark = ctx.store.conn.dialect.placeholder
    sql = f"SELECT {', '.join(cols)} FROM attempts WHERE task_id = {mark}"
    return _attempt_rows(cols, ctx.store.conn.query_all(sql, (task,)))


def _attempt_rows(cols: Sequence[str], rows: Iterable[Sequence[object]]) -> list[AttemptRow]:
    """Each selected `attempts` row by column name. A row the store holds malformed is still an attempt: it is kept,
    with its text columns read as text and no cause (`malformed_attempt_row`), and logged. A dropped row would let a
    repeat through: `rescue_select.eligible` counts the `rescue_failed` rows, the attempt cap the causes."""
    read: list[AttemptRow] = []
    for row in rows:
        raw = dict(zip(cols, row, strict=True))
        try:
            read.append(attempt_row(raw))
        except MalformedRecord as exc:
            epic_log.warning("attempts row kept with no cause: %s", exc)
            read.append(malformed_attempt_row(raw))
    return read


def malformed_attempt_row(raw: Mapping[str, object]) -> AttemptRow:
    """Pure. A malformed `attempts` row as an attempt with no cause: each selected text column as text (None when
    unset), `seq` when it is an int. Nothing selected is dropped, so the row still counts where rows are counted."""

    def text(key: str) -> str | None:
        value = raw.get(key)
        return None if value is None else str(value)

    seq = raw.get("seq")
    row: AttemptRow = {"run_id": text("run_id") or "", "phase_id": text("phase_id"), "cause": None}
    if isinstance(seq, int) and not isinstance(seq, bool):
        row["seq"] = seq
    for key in ("kind", "reason", "ts"):
        if key in raw:
            row[key] = text(key)
    return row


def _run_number(run: str) -> int:
    """The number a run id ends in, `x-3` is 3; -1 for a run id that ends in none."""
    tail = run.rsplit("-", 1)[-1]
    return int(tail) if "-" in run and tail.isdigit() else -1


def _record_initiative(run: str, record: Mapping[str, object]) -> str:
    """The initiative a record belongs to: its own `initiative`, else its run id less the run number."""
    return str(record.get("initiative") or "") or (run.rsplit("-", 1)[0] if _run_number(run) >= 0 else run)


def _record_attempt(run: str, phase: str, record: TaskRecord, ts: str) -> Attempt | None:
    """One attempt from one run task record that names a cause, None for a record that names none.

    The cause, reason, kind and the body it was built on are `core.run_record`'s; the record's own verdicts ride beside
    them, which is what triage reads from "the task records of its attempts" (docs/design/triage.md §1). `ts` is the
    record's `date`, else `ts`."""
    # Not module level: importing the driver loads no more of core than it did, so a process that imports it alone
    # (the import-cost tests run one) resolves the same core modules it always has.
    from core import run_record

    attempt = run_record.record_attempt(record)
    if attempt is None:
        return None
    fields = as_mapping(record)
    build = as_mapping(fields.get("build"))
    evidence = fields.get("evidence") or build.get("evidence")
    verdicts = {key: fields[key] for key in ("review", "adversary", "arbitration", "fix_loop") if isinstance(fields.get(key), Mapping)}
    sha = run_record.record_body_sha(record)
    return as_attempt({
        "run": run, "phase": phase, **attempt, "ts": str(record.get("date") or "") or ts, **verdicts,
        **({"evidence": list(evidence)} if is_list(evidence) else {}),
        **({"body_sha": sha} if sha else {}),
    })


def _refused_attempt(run: str, phase: str, raw: object, why: str, ts: str) -> Attempt:
    """The attempt a task record stands for when `task_record` refused it: never dropped, since under work_state store
    the records are the attempts the cap counts. It keeps the record's cause when that is text, reads none of its
    verdicts, and names what was wrong in `malformed`; `unreadable_reason` holds a task such an attempt leaves unsure."""
    from core import run_record  # not module level: see `_record_attempt`

    fields = as_mapping(raw)
    cause, cause_why, date = fields.get("cause"), fields.get("cause_why"), fields.get("date")
    named = cause.strip() if isinstance(cause, str) else ""
    because = cause_why.strip() if isinstance(cause_why, str) else ""
    kind = run_record.record_kind(because) if named else ""
    return as_attempt({
        "run": run, "phase": phase, "ts": date if isinstance(date, str) and date else ts, "malformed": why,
        "reason": f"the task record of run {run} is malformed, so its verdicts were not read: {why}",
        **({"cause": named, "cause_why": because} if named else {}),
        **({"kind": kind} if kind else {}),
    })


def unreadable_reason(found: Sequence[tuple[Mapping[str, object], object]]) -> str | None:
    """Pure. Why a task's run task records (`caused_records`, oldest first) leave its next build unsafe, else None.

    The newest record refused: a rebuild would resume from an older attempt's patch. A refused record with no cause:
    it cannot be told apart from a record that names none, which is no attempt, so the cap cannot count it."""
    if found and "malformed" in found[-1][0]:
        newest = found[-1][0]
        return (
            f"the newest task record (run {newest.get('run')}) is malformed ({newest.get('malformed')}); a rebuild would "
            "resume from an older attempt's patch, so a person decides"
        )
    causeless = next((a for a, _ in found if "malformed" in a and not a.get("cause")), None)
    if causeless is not None:
        return (
            f"the task record of run {causeless.get('run')} is malformed ({causeless.get('malformed')}) and names no cause, "
            "so the attempt cap cannot count it; a person decides"
        )
    return None


def caused_records(ctx: Ctx, phase: str, task: str) -> list[tuple[Attempt, TaskRecord]]:
    """(attempt, record) for each of the task's run task records that names a cause, oldest first.

    A record `task_record` refuses, a record file that is not JSON or cannot be read, and a store row whose `record_json`
    is not an object are never dropped: each is an attempt with no
    verdicts and an empty record (`_refused_attempt`), whether or not it names a cause.

    Under work_state store nothing writes an attempt into the work file: these records are the attempts. This machine's
    record files under the runs directory are read first; the store's copy (`task_records`) adds the runs another lane
    recorded. Only this initiative's records count. A read that fails is logged and gives what was read before it.

    A run's number orders them, its `ts` only within one run: one initiative's runs never overlap (its
    `runs:<initiative>` lease), and the minimal record an invoke failure leaves has no date, only its file's time."""
    found: dict[str, tuple[TaskRecord | _Refused, str]] = {}
    for path in sorted(Path(ctx.runs_dir).glob(f"*/tasks/{phase}/{task}.json")):
        found[path.parts[-4]] = _read_record_file(path)
    if ctx.store is not None:
        try:
            from harness import store_read  # not module level: see `mirror_read`

            for row in store_read.task_records_of(ctx.store.conn, phase, task):
                stored = row["record_json"]
                record = (
                    _checked_record(stored)
                    if isinstance(stored, Mapping)
                    else _Refused(stored, f"record_json is {type(stored).__name__}, not an object")
                )
                run, held, updated = str(row["run_id"]), found.get(str(row["run_id"])), str(row.get("updated_at") or "")
                # The file is read first; the store's copy stands in when there is no file, or when the file was refused
                # and the copy is provably the same record or newer (`stands_in`). Otherwise the refused file stays.
                if held is None or (isinstance(held[0], _Refused) and stands_in(record, updated, held[1])):
                    found[run] = (record, updated)
        except Exception as exc:
            epic_log.warning("task records of %s/%s not read from the store: %s: %s", phase, task, type(exc).__name__, exc)
    caused: list[tuple[int, str, str, Attempt, TaskRecord]] = []
    for run, (record, stamp) in found.items():
        if isinstance(record, _Refused):
            if _record_initiative(run, as_mapping(record.raw)) == ctx.initiative_id:
                epic_log.warning("task record of %s/%s in %s is malformed, read as a causal attempt: %s", phase, task, run, record.why)
                refused = _refused_attempt(run, phase, record.raw, record.why, stamp)
                caused.append((_run_number(run), refused["ts"], stamp, refused, {}))
        elif _record_initiative(run, record) == ctx.initiative_id and (attempt := _record_attempt(run, phase, record, stamp)):
            caused.append((_run_number(run), attempt["ts"], stamp, attempt, record))
    return [(attempt, record) for *_, attempt, record in sorted(caused, key=lambda t: t[:3])]


@dataclass(frozen=True)
class _Refused:
    """A task record `task_record` refused: what it held, and why."""

    raw: object
    why: str


def _checked_record(raw: object) -> TaskRecord | _Refused:
    try:
        return task_record(raw)
    except MalformedRecord as exc:
        return _Refused(raw, str(exc))


def _read_record(text: str) -> TaskRecord | _Refused:
    """A record file's text as a task record, or refused: a file that is not JSON is refused too."""
    try:
        raw = json.loads(text)
    except ValueError as exc:
        return _Refused(None, f"not JSON: {exc}")
    return _checked_record(raw)


def stands_in(stored: TaskRecord | _Refused, stored_at: str, file_at: str) -> bool:
    """Pure. Whether the store's copy of a run's task record may replace the record file that was refused.

    Only a copy that reads, names a cause, and was written no earlier than the file: a crash while the file was being
    re-saved with its cause leaves a truncated file beside an older copy with no cause, and that copy would read as no
    attempt at all. Kept refused, the record holds the task (`unreadable_reason`) instead."""
    if isinstance(stored, _Refused) or not (isinstance(stored.get("cause"), str) and str(stored.get("cause")).strip()):
        return False
    try:
        return datetime.fromisoformat(stored_at) >= datetime.fromisoformat(file_at)
    except (TypeError, ValueError):  # a time that does not parse, or one with a zone beside one without
        return False


def _read_record_file(path: Path) -> tuple[TaskRecord | _Refused, str]:
    """A record file as (record, its file time). A file that cannot be read is refused, never skipped: it is a run's
    attempt all the same. Its time is "" when even its stat fails."""
    try:
        stamp = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
    except OSError:
        stamp = ""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _Refused(None, f"the record file could not be read: {type(exc).__name__}: {exc}"), stamp
    return _read_record(text), stamp


def record_attempts(ctx: Ctx, phase: str, task: str) -> list[Attempt]:
    """The task's attempts as its runs' task records hold them (`caused_records`), oldest first: for triage of an item
    with no attempt, which would otherwise reach triage with nothing to cite."""
    return [attempt for attempt, _ in caused_records(ctx, phase, task)]


def _record_rescue_gate(ctx: Ctx, phase: str, move: Mapping[str, object], diff: Mapping[str, object], minutes: float) -> None:
    """Ledger the gate's decision on a rescue's move, as `run_phase_tasks` ledgers a phase batch's.

    Keyed per task, since the store ignores a second gate row under the same run, phase and seq.
    """
    task = str(move["target"])
    manifest = build_manifest(
        run_id=f"{ctx.run_id}:{phase}:{task}",
        ts=utc_now(),
        principal=RESCUE_PRINCIPAL,
        cartridge=ctx.cartridge,
        provider_profile=ctx.provider_profile,
        proposals=[dict(move)],
        gate_diffs=[dict(diff)],
        human_minutes=minutes,
        totals={"rescued": 1, "auto_applied": 0, "gated": 1},
    )
    if fence_reason(ctx) is not None:
        return
    append_ledger(manifest, ledger_path=ctx.ledger_path)  # the host file is written once, never inside a retry
    if ctx.store is not None:
        store = ctx.store
        for row in ledger.read(ctx.ledger_path):
            if row.get("run_id") == manifest["run_id"]:
                ctx.retry.write(lambda row=row: store.record_ledger(row, epoch=ctx.epoch), END_WRITE_WINDOW_S)
        ctx.retry.write(
            lambda: store.record_gate_decisions(ctx.run_id, f"{phase}:{task}", [diff], epoch=ctx.epoch),
            END_WRITE_WINDOW_S,
        )


def policy_split(
    ctx: Ctx, proposals: list[dict[str, object]]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """The policy split for one gate batch. With a store the streaks come from its ledger table, so a lane host
    judges a kind by the streak every host shares; the host file is then only an export."""
    return split_by_policy(
        proposals,
        cartridge=dict(ctx.cartridge),
        ledger_path=ctx.ledger_path,
        provider_profile=ctx.provider_profile,
        store=ctx.store,
    )


def _rescue_move(ctx: Ctx, item: Mapping[str, object], *, phase: str, evidence: list[dict[str, object]]) -> tuple[bool, str]:
    """The approved task's `state_move`, through `split_by_policy` and `gate` as a build's is.

    Executed on the built-in workstore arm whatever arm the cartridge names, so the write is code and never
    a model call. The caller has already checked there is a work file or an authoritative store to write.
    """
    task = str(item["id"])
    move = {
        **proposal(
            ctx.cartridge,
            kind="state_move",
            target=task,
            evidence=evidence,
            rationale=f"{task} was rescued: its kept patch passed the harness's checks and review",
            suggested_action=f"mark {task} approved",
        ),
        **state_move_apply(item_path(item), "approved"),
    }
    auto, gated = policy_split(ctx, [move])
    decisions, minutes = gate(gated, assume=ctx.assume)
    decision, edited = ("approved", False) if auto else (decisions[0][1], decisions[0][2])
    kinds = as_mapping(ctx.cartridge.get("write_kinds"))
    code_arm = replace(
        ctx,
        cartridge={**ctx.cartridge, "write_kinds": {**kinds, "state_move": {**as_mapping(kinds.get("state_move")), "apply_arm": "workstore"}}},
    )
    # A rescue lands no draft and merges nothing: its kept work is the `agents/<run>/<task>` branch `_land_branch` made.
    state = Execution(landed={}, merged={}, moved={}, quarantined=[], escalated=frozenset({task}))
    applied, detail = (
        execute_cleared(code_arm, move, slot="state_move", subject=task, phase=phase, state=state, by_id={task: cast(EpicItem, dict(item))})
        if decision == "approved"
        else (False, "the gate refused the move")
    )
    if gated:
        _record_rescue_gate(ctx, phase, move, gate_diff(move, decision, applied=applied, edited=edited), minutes)
    return applied, detail


def _commit_patch(ctx: Ctx, worktree: Path, patch: str, task: str) -> str | None:
    """Apply `patch` in `worktree` and commit it as `epic <run>: <task>`. None on success, else why not."""
    link_venv(ctx.repo, worktree)
    ok, detail = apply_patch(patch, worktree)
    if not ok:
        return f"kept patch did not apply: {detail}"
    ok, detail = git_in(*IDENTITY, "-C", str(worktree), "add", "-A")
    if ok:
        ok, detail = git_in(
            *IDENTITY, "-C", str(worktree), "commit", "--allow-empty", "-q", "-m", f"epic {ctx.run_id}: {task}"
        )
    return None if ok else f"kept patch could not be committed: {detail}"


def _land_branch(ctx: Ctx, *, task: str, patch: str) -> str | None:
    """`agents/<run>/<task>`: one commit on `default_ref` holding `patch`, the shape `cox runs land` takes.

    None on success. Otherwise a harness-fault reason, with any half-made branch deleted.
    """
    branch = ctx.scratch_branch(task)
    worktree = ctx.worktree_root / ctx.run_id / f"land-{task}"
    ok, detail = create_worktree(ctx.repo, worktree, branch=branch, base=ctx.default_ref)
    if not ok:
        return f"{HARNESS_FAULT_PREFIX} branch {branch} could not be created: {detail}"
    try:
        why = _commit_patch(ctx, worktree, patch, task)
    finally:
        remove_worktree(ctx.repo, worktree)
    if why is not None:
        git_in("-C", str(ctx.repo), "branch", "-D", branch)
        return f"{HARNESS_FAULT_PREFIX} {why}"
    return None


def rescue_task(ctx: Ctx, item: Mapping[str, object], *, phase: str) -> dict[str, object]:
    """Harness checks on a quarantined task's kept patch, one review round, then a gated move to `approved` or `rescue_failed`."""
    task = str(item["id"])
    attempts = as_list(item.get("attempts"))
    # Under work_state store no work file holds an attempt: the runs' task records are the attempts, and the newest one
    # holds the kept patch (the store's copy, when another lane wrote it). Reading only the file, the rescue the chair
    # launched from those records would find nothing, and the chair would launch it again.
    records = caused_records(ctx, phase, task) if not attempts and store_authoritative(ctx) else []
    if records:
        item = {**item, "attempts": [attempt for attempt, _ in records]}
        saved: Mapping[str, object] | None = records[-1][1]
    else:
        saved = load_result(ctx.runs_dir, str(as_mapping(attempts[-1])["run"]), phase, task) if attempts else None
    patch = rescue_select.patch_of(saved)
    caused = with_stored_causes(item, stored_causes(ctx, task))
    ok, why = rescue_select.eligible(with_stored_rescues(caused, _stored_rescues(ctx, task)), patch)
    if not ok or patch is None:
        return {"status": "not_eligible", "why": why}
    if not item.get("path") and not store_authoritative(ctx):
        # With nothing to write in code, the workstore arm would fall back to a model call.
        return {"status": "not_eligible", "why": "no work file to move and no authoritative store"}

    def apply(worktree: Path, text: str) -> str | None:
        link_venv(ctx.repo, worktree)  # as `build_task` does, so the checks find the same interpreter
        applied, detail = apply_patch(text, worktree)
        return None if applied else detail

    try:
        verified = rescue_checks.verify_patch(
            ctx.repo,
            ctx.default_ref,
            patch,
            ctx.checks,
            apply=apply,
            workdir=ctx.worktree_root / ctx.run_id,
            env=checks_env(ctx, {}, phase, f"@rescue-{task}"),
        )
    finally:
        # A rescue is one task under a run id of its own, so the run's whole temp directory goes with it.
        shutil.rmtree(run_scratch(ctx), ignore_errors=True)
    if not verified["passed"]:
        return _rescue_failed(ctx, item, phase=phase, reason=str(verified["reason"]), failed_checks=True)

    rows: list[dict[str, object]] = verified["rows"]
    evidence = [*as_list((saved or {}).get("evidence")), *rows]
    save_task_result(
        ctx,
        {**without_cause(saved or {}), "initiative": ctx.initiative_id, "phase": phase, "evidence": evidence, **body_stamp(item)},
        phase=phase,
        task=task,
    )
    graph: Mapping[str, object] = rescue_review.run(
        {
            "run_id": f"{ctx.run_id}:{phase}:{task}",
            "date": ctx.date,
            "ticket": task,
            "ticket_title": item.get("title") or "",
            "ticket_body": item.get("body") or "",
            "cartridge": ctx.cartridge,
            "surfaces": as_list(item.get("surfaces")),
            "patterns": as_list(item.get("patterns")),
            "patch": patch,
            "evidence": rows,
        },
        ctx.runner,
    )
    # The rescue's review ran whatever it decided: its steps are written before any return below.
    record_steps(ctx, graph, phase=phase, task=task)
    if graph.get("failed_node") or as_mapping(graph.get("fix_loop")).get("review_placeholder"):
        return {"status": "review_failed", "why": str(graph.get("failed_node_reason") or "review placeholders")}
    if graph.get("verdict") != "approve":
        rationale = str(as_mapping(graph.get("review")).get("rationale") or "no rationale given")
        return _rescue_failed(ctx, item, phase=phase, reason=f"rescue review revised: {rationale}", failed_checks=False)

    # The branch precedes the gated move, so a refused move leaves it in place for the chair to land.
    land_fault = _land_branch(ctx, task=task, patch=patch)
    if land_fault is not None:
        return _rescue_failed(ctx, item, phase=phase, reason=land_fault, failed_checks=False)
    save_task_result(
        ctx, {**graph, "initiative": ctx.initiative_id, "phase": phase, "evidence": evidence, **body_stamp(item)}, phase=phase, task=task
    )
    applied, detail = _rescue_move(
        ctx,
        item,
        phase=phase,
        evidence=[
            {"check": "review_charter verdict", "output": str(as_mapping(graph.get("review")).get("verdict"))},
            *({"check": row["command"], "output": row["output"]} for row in rows),
        ],
    )
    if not applied:
        record_tasks(ctx, phase, [], [{"id": task, "grain": "task"}])
        return {"status": "move_refused", "why": detail}
    record_tasks(ctx, phase, [{"id": task, "status": "approved", "state": "approved"}], [])
    return {"status": "approved", "detail": detail}
