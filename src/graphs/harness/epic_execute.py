"""The epic driver, executing what was cleared: merges, records, and the rescue of a quarantined task."""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from core import ledger, workstore
from core.manifest import append_ledger, build_manifest, gate_diff
from graphs._contract import proposal
from graphs.delivery import rescue_review
from harness import rescue_checks, rescue_select
from harness.autonomy import split_by_policy
from harness.checks import HARNESS_FAULT_PREFIX
from harness.epic_batch import _draft_note, _quarantine_task, _unkept_work, state_move_apply
from harness.epic_branches import (
    _IDENTITY,
    _checks_env,
    _fetch_phase_branches,
    _open_review_pr,
    _push_review_branch,
    _run_scratch,
    opens_pr,
)
from harness.epic_build import _body_stamp
from harness.epic_context import _Ctx, _git
from harness.epic_phase_branch import _branch_exists, _locked_merges, _publish_branch, _rebase
from harness.epic_records import (
    _END_WRITE_WINDOW_S,
    _ctx_store,
    _Execution,
    _fenced,
    _log,
    _mirror_by,
    _mirror_write,
    _now,
    _record_tasks,
    _refuse_stale,
    _repair_file_state,
    _save_result,
    _store_authoritative,
    _store_first,
    _wait_if_paused,
)
from harness.gate import apply_arm_for, auto_apply, gate
from harness.handoff import build_handoff
from harness.phase_remote import Kind, missing_branch_cause
from harness.resume import load_result
from harness.store_write import record_handoff, record_merge
from harness.worktree import apply_patch, create_worktree, link_venv, remove_worktree
from runner.protocol import LimitStop, RunnerError


def _without_cause(saved: Mapping[str, Any]) -> dict[str, Any]:
    """A saved result as a later run reuses it. Its cause, and the body it was quarantined on, name the run that
    quarantined it, not this one."""
    return {k: v for k, v in saved.items() if k not in ("cause", "cause_why", "body_sha")}


def _execute(
    ctx: _Ctx,
    item: Mapping[str, Any],
    *,
    slot: str,
    subject: str,
    phase: str,
    state: _Execution,
    by_id: Mapping[str, dict[str, Any]],
) -> tuple[bool, str]:
    """Do what the gate — or the policy — cleared. Dispatch on the KIND, not the slot.

    The kind is what governs, and escalation rewrites it: a `draft_pr_create`
    whose patch touched governance arrives here as `self_modification`, falls
    through to the arm the cartridge names (`pr`, which has no executor here),
    and reports honestly that nothing happened. That is what makes an escalated
    task's merge impossible to earn rather than merely discouraged.
    """
    stale = _fenced(ctx)
    if stale is not None:
        _refuse_stale(state, phase=phase, subject=subject, slot=slot, reason=stale)
        return False, stale
    _wait_if_paused(ctx)
    stale = _fenced(ctx)  # the wait can outlast this driver's lease
    if stale is not None:
        _refuse_stale(state, phase=phase, subject=subject, slot=slot, reason=stale)
        return False, stale

    kind = item.get("kind")

    if kind == "draft_pr_create" and slot == "draft":
        # Landing the draft IS creating the branch. There is no forge arm here,
        # so the "draft PR" is a local branch until one exists — and a branch
        # nobody has opened has exactly the blast radius the taxonomy prices it
        # at: the cost of a wrong one is a branch nobody reads.
        draft = ctx.draft_branch(phase, subject)
        ok, detail = _git("-C", str(ctx.repo), "branch", draft, ctx.scratch_branch(subject))
        state.landed[subject] = ok
        if not ok:
            state.draft_detail[subject] = f"draft branch {draft} was not created ({detail})"
        else:
            state.push_warnings.extend(filter(None, [_publish_branch(ctx, draft)]))
        return ok, detail or f"created {draft}"

    if kind == "merge_stack" and slot == "merge":
        # Task branch -> its parent PHASE branch, and nothing else. No path in
        # this driver merges to a default branch: `merge_main` is never emitted,
        # never executed, and unreachable from here at any comfort level and on
        # any streak. The swarm's output is branches and drafts.
        if not state.landed.get(subject):
            return False, "the draft did not land, so its merge is refused with it"
        draft = ctx.draft_branch(phase, subject)
        if not _branch_exists(ctx, draft):
            # A draft another host created and pushed: make it local, or refuse with the cause.
            found = _fetch_phase_branches(ctx, [draft])[draft]
            if found.kind in (Kind.MISSING_EVERYWHERE, Kind.FETCH_FAILED):
                return False, missing_branch_cause(draft, found)
        ok, detail = _git(
            *_IDENTITY, "-C", str(ctx.phase_worktree(phase)), "merge", "--no-ff", draft,
            "-m", f"epic {ctx.run_id}: merge {subject} into {phase}",
        )
        if not ok:
            _git("-C", str(ctx.phase_worktree(phase)), "merge", "--abort")
            state.quarantined.append(
                _quarantine_task(
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
            head_ok, head = _git("-C", str(ctx.phase_worktree(phase)), "rev-parse", "HEAD")
            if head_ok:
                state.commits[subject] = head
            state.push_warnings.extend(filter(None, [_publish_branch(ctx, ctx.phase_branch(phase))]))
        return ok, detail

    if kind == "stack_rebase" and slot == "rebase":
        ok, detail = _rebase(ctx, phase, _rebase_base(item), _locked_merges(ctx, by_id.values(), phase))
        if not ok:
            state.quarantined.append(
                {"id": phase, "phase": phase, "grain": "phase", "reason": f"rebase conflict: {detail}"}
            )
        return ok, detail

    if kind == "self_modification" and slot == "draft" and item.get("escalated_from") == "draft_pr_create":
        # A review PR a human reads, never a merge: no merge call runs here. It is a task PR, so it follows land_at.
        if not opens_pr(ctx.land_at, "task"):
            return False, f"escalated: its review PR opens only at land_at task, not {ctx.land_at}"
        ok, detail = _push_review_branch(ctx, subject)
        if not ok:
            return False, f"review branch push failed: {detail}"
        paths = next(
            (str(row.get("output") or "") for row in item.get("evidence") or [] if row.get("check") == "governance_paths"),
            "",
        )
        body = "\n".join(
            (
                f"Review verdict: {item.get('review_verdict') or 'none'}",
                f"Arbiter verdict: {item.get('arbitration_verdict') or 'none'}",
                f"Governance paths: {paths or 'none'}",
            )
        )
        ok, detail = _open_review_pr(
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
        why = _unkept_work(ctx, phase, subject, state)
        # A draft that did not land is not lost work: the task branch holds it and `runs land` opens the PR.
        # On a lane host that branch is in the host's repo, so it counts there and is fetched to the chair later.
        note = _draft_note(ctx, subject, state) if why is not None else None
        if note is not None:
            state.notes[subject] = note
            why = None
        if why is not None:
            state.moved[subject] = False
            if not any(q.get("id") == subject and q.get("grain") == "task" for q in state.quarantined):
                state.quarantined.append(
                    _quarantine_task(
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
    if slot == "state_move" and _store_authoritative(ctx) and by_id.get(subject) is not None:
        # The store's acceptance is the move. The file is a cache, so no arm runs; the driver writes its line.
        refused = _store_first(ctx, by_id[subject], "approved")
        if refused is not None:
            state.moved[subject] = False
            return False, refused
        if not _repair_file_state(by_id[subject], "approved"):
            _log.warning("store moved %s to approved; file state line not written, regenerate-states repairs it", subject)
        state.moved[subject] = True
        _record_merge(ctx, state, subject)
        return True, "store moved to approved; file state line written"
    try:
        applied, detail = auto_apply(
            dict(item), cartridge=cast(dict[str, Any], ctx.cartridge), runner=ctx.runner, wait_if_paused=lambda: _wait_if_paused(ctx)
        )
    except LimitStop:
        raise  # the account's own limit, not this task's; never through `_quarantine_task`
    except RunnerError as exc:
        arm = apply_arm_for(cast(str, item.get("kind")), cast(dict[str, Any], ctx.cartridge))
        reason = f"apply arm '{arm}' raised {type(exc).__name__}: {exc}"
        state.quarantined.append(_quarantine_task(ctx, by_id, phase=phase, task=subject, reason=reason, kind="infra"))
        return False, reason
    if slot == "state_move":
        state.moved[subject] = applied
        if applied:
            _mirror_write(ctx, by_id.get(subject), "approved")
    return applied, detail


def _record_merge(ctx: _Ctx, state: _Execution, task: str) -> None:
    """work_state: store. Stamp the merge commit and the handoff on the task's approved row. Errors are logged, never raised.

    Runs after the move to approved, because `record_merge` refuses any other state.
    """
    commit = state.commits.get(task)
    if commit is None or not state.merged.get(task):
        return
    try:
        handoff = build_handoff(state.builds.get(task) or {}, ctx.scratch_branch(task), commit)
        record_merge(_ctx_store(ctx).conn, ctx.initiative_id, task, commit, _now(), _mirror_by(ctx))
        record_handoff(_ctx_store(ctx).conn, ctx.initiative_id, task, handoff, _now(), _mirror_by(ctx))
    except Exception as exc:
        _log.warning("merge and handoff not written to the store row: task=%s: %s: %s", task, type(exc).__name__, exc)
        return
    state.handoffs[task] = handoff


def _rebase_base(item: Mapping[str, Any]) -> str:
    """The ref a `stack_rebase` proposal names as the ground that moved.

    Read back off the proposal rather than recomputed, so what executes is the
    thing the gate was shown: a rebase onto a ref nobody approved is a different
    write from the one that was decided.
    """
    return str(item.get("suggested_action") or "").rsplit(" onto ", 1)[-1].strip()


# ── rescue: a kept patch the harness, not the builder, failed to land ────────


RESCUE_PRINCIPAL = "epic-swarm(rescue-review)"


def _rescue_failed(ctx: _Ctx, item: Mapping[str, Any], *, phase: str, reason: str, failed_checks: bool) -> dict[str, Any]:
    """The task stays quarantined: a `rescue_failed` attempt carrying the rule's cause, and a quarantined task row."""
    task = str(item["id"])
    cause, cause_why = rescue_select.rescue_cause(failed_checks, reason)
    entry = _quarantine_task(
        ctx, {task: dict(item)}, phase=phase, task=task, reason=reason, kind="rescue_failed", cause=(cause, cause_why)
    )
    _record_tasks(ctx, phase, [], [entry])
    return {"status": "rescue_failed", "reason": reason, "cause": cause}


def records_after(found: Sequence[tuple[Mapping[str, Any], Any]], cutoff: Any) -> list[tuple[Mapping[str, Any], Any]]:
    """Pure. The (attempt, record) pairs whose run counts after the fresh-start `cutoff` (`workstore.counts_after`)."""
    return [(attempt, record) for attempt, record in found if workstore.counts_after(attempt.get("run"), cutoff)]


def with_record_attempts(item: Mapping[str, Any], recorded: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Pure. `item` with `recorded` (its runs' task records as attempts, oldest first) as its attempts, when it holds
    none of its own; an item whose work file holds attempts, or with nothing recorded, is returned as it is."""
    return {**item, "attempts": [dict(a) for a in recorded]} if recorded and not item.get("attempts") else item


def with_stored_causes(item: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """`item` with each causeless file attempt given the cause of the store row for its run and phase, newest seq."""
    newest = {(r["run_id"], r["phase_id"]): r["cause"] for r in sorted(rows, key=lambda r: r["seq"])}
    return {
        **item,
        "attempts": [
            {**a, "cause": newest[key]} if not a.get("cause") and newest.get(key) else a
            for a in item["attempts"]
            for key in [(a.get("run"), a.get("phase"))]
        ],
    }


def with_stored_rescues(item: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """`item` with the store's `rescue_failed` attempts folded into its own, in ts order.

    core's ATTEMPT_KINDS has no `rescue_failed`, so the work file never holds one and the store row is the
    record. A row no older than the first attempt on the current body is stamped with that body's sha.
    """
    current = workstore.body_sha(item["body"])
    since = min((str(a.get("ts") or "") for a in item["attempts"] if a.get("body_sha", current) == current), default=None)
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
    return {**item, "attempts": sorted([*item["attempts"], *stored], key=lambda a: str(a.get("ts") or ""))}


def _stored_rescues(ctx: _Ctx, task: str) -> list[dict[str, Any]]:
    """The store's `rescue_failed` attempts for `task`, any run. Empty without a store."""
    if ctx.store is None:
        return []
    cols = ("run_id", "phase_id", "kind", "reason", "ts", "cause")
    mark = ctx.store.conn.dialect.placeholder
    sql = f"SELECT {', '.join(cols)} FROM attempts WHERE task_id = {mark} AND kind = {mark}"
    return [dict(zip(cols, row, strict=True)) for row in ctx.store.conn.query_all(sql, (task, rescue_select.RESCUE_KIND))]


def _stored_causes(ctx: _Ctx, task: str) -> list[dict[str, Any]]:
    """The store's attempt causes for `task`, any run and kind. Empty without a store."""
    if ctx.store is None:
        return []
    cols = ("run_id", "phase_id", "seq", "cause")
    mark = ctx.store.conn.dialect.placeholder
    sql = f"SELECT {', '.join(cols)} FROM attempts WHERE task_id = {mark}"
    return [dict(zip(cols, row, strict=True)) for row in ctx.store.conn.query_all(sql, (task,))]


def _run_number(run: str) -> int:
    """The number a run id ends in, `x-3` is 3; -1 for a run id that ends in none."""
    tail = run.rsplit("-", 1)[-1]
    return int(tail) if "-" in run and tail.isdigit() else -1


def _record_initiative(run: str, record: Mapping[str, Any]) -> str:
    """The initiative a record belongs to: its own `initiative`, else its run id less the run number."""
    return str(record.get("initiative") or "") or (run.rsplit("-", 1)[0] if _run_number(run) >= 0 else run)


def _record_attempt(run: str, phase: str, record: Mapping[str, Any], ts: str) -> dict[str, Any] | None:
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
    build = cast(Mapping[str, Any], record.get("build") if isinstance(record.get("build"), Mapping) else {})
    evidence = record.get("evidence") or build.get("evidence")
    verdicts = {key: record[key] for key in ("review", "adversary", "arbitration", "fix_loop") if isinstance(record.get(key), Mapping)}
    sha = run_record.record_body_sha(record)
    return {
        "run": run, "phase": phase, **attempt, "ts": str(record.get("date") or "") or ts, **verdicts,
        **({"evidence": list(evidence)} if isinstance(evidence, list) else {}),
        **({"body_sha": sha} if sha else {}),
    }


def _caused_records(ctx: _Ctx, phase: str, task: str) -> list[tuple[dict[str, Any], Mapping[str, Any]]]:
    """(attempt, record) for each of the task's run task records that names a cause, oldest first.

    Under work_state store nothing writes an attempt into the work file: these records are the attempts. This machine's
    record files under the runs directory are read first; the store's copy (`task_records`) adds the runs another lane
    recorded. Only this initiative's records count. A read that fails is logged and gives what was read before it.

    A run's number orders them, its `ts` only within one run: one initiative's runs never overlap (its
    `runs:<initiative>` lease), and the minimal record an invoke failure leaves has no date, only its file's time."""
    found: dict[str, tuple[Mapping[str, Any], str]] = {}
    for path in sorted(Path(ctx.runs_dir).glob(f"*/tasks/{phase}/{task}.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            stamp = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
        except (OSError, ValueError):
            continue
        if isinstance(record, Mapping):
            found[path.parts[-4]] = (record, stamp)
    if ctx.store is not None:
        try:
            from harness import store_read  # not module level: see `_mirror_read`

            for row in store_read.task_records_of(ctx.store.conn, phase, task):
                if isinstance(row["record_json"], Mapping):
                    found.setdefault(str(row["run_id"]), (row["record_json"], str(row.get("updated_at") or "")))
        except Exception as exc:
            _log.warning("task records of %s/%s not read from the store: %s: %s", phase, task, type(exc).__name__, exc)
    caused = [
        (_run_number(run), attempt["ts"], stamp, attempt, record)
        for run, (record, stamp) in found.items()
        if _record_initiative(run, record) == ctx.initiative_id and (attempt := _record_attempt(run, phase, record, stamp)) is not None
    ]
    return [(attempt, record) for *_, attempt, record in sorted(caused, key=lambda t: t[:3])]


def _record_attempts(ctx: _Ctx, phase: str, task: str) -> list[dict[str, Any]]:
    """The task's attempts as its runs' task records hold them (`_caused_records`), oldest first: for triage of an item
    with no attempt, which would otherwise reach triage with nothing to cite."""
    return [attempt for attempt, _ in _caused_records(ctx, phase, task)]


def _record_rescue_gate(ctx: _Ctx, phase: str, move: Mapping[str, Any], diff: Mapping[str, Any], minutes: float) -> None:
    """Ledger the gate's decision on a rescue's move, as `_run_phase` ledgers a phase batch's.

    Keyed per task, since the store ignores a second gate row under the same run, phase and seq.
    """
    task = str(move["target"])
    manifest = build_manifest(
        run_id=f"{ctx.run_id}:{phase}:{task}",
        ts=_now(),
        principal=RESCUE_PRINCIPAL,
        cartridge=ctx.cartridge,
        provider_profile=ctx.provider_profile,
        proposals=[dict(move)],
        gate_diffs=[dict(diff)],
        human_minutes=minutes,
        totals={"rescued": 1, "auto_applied": 0, "gated": 1},
    )
    if _fenced(ctx) is not None:
        return
    append_ledger(manifest, ledger_path=ctx.ledger_path)  # the host file is written once, never inside a retry
    if ctx.store is not None:
        store = ctx.store
        for row in ledger.read(ctx.ledger_path):
            if row.get("run_id") == manifest["run_id"]:
                ctx.retry.write(lambda row=row: store.record_ledger(row, epoch=ctx.epoch), _END_WRITE_WINDOW_S)
        ctx.retry.write(
            lambda: store.record_gate_decisions(ctx.run_id, f"{phase}:{task}", [diff], epoch=ctx.epoch),
            _END_WRITE_WINDOW_S,
        )


def _policy_split(ctx: _Ctx, proposals: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The policy split for one gate batch. With a store the streaks come from its ledger table, so a lane host
    judges a kind by the streak every host shares; the host file is then only an export."""
    return split_by_policy(
        proposals,
        cartridge=cast(dict[str, Any], ctx.cartridge),
        ledger_path=ctx.ledger_path,
        provider_profile=ctx.provider_profile,
        store=ctx.store,
    )


def _rescue_move(ctx: _Ctx, item: Mapping[str, Any], *, phase: str, evidence: list[dict[str, Any]]) -> tuple[bool, str]:
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
        **state_move_apply(item.get("path"), "approved"),
    }
    auto, gated = _policy_split(ctx, [move])
    decisions, minutes = gate(gated, assume=ctx.assume)
    decision, edited = ("approved", False) if auto else (decisions[0][1], decisions[0][2])
    kinds = ctx.cartridge.get("write_kinds") or {}
    code_arm = replace(
        ctx,
        cartridge={**ctx.cartridge, "write_kinds": {**kinds, "state_move": {**(kinds.get("state_move") or {}), "apply_arm": "workstore"}}},
    )
    # A rescue lands no draft and merges nothing: its kept work is the `agents/<run>/<task>` branch `_land_branch` made.
    state = _Execution(landed={}, merged={}, moved={}, quarantined=[], escalated=frozenset({task}))
    applied, detail = (
        _execute(code_arm, move, slot="state_move", subject=task, phase=phase, state=state, by_id={task: dict(item)})
        if decision == "approved"
        else (False, "the gate refused the move")
    )
    if gated:
        _record_rescue_gate(ctx, phase, move, gate_diff(move, decision, applied=applied, edited=edited), minutes)
    return applied, detail


def _commit_patch(ctx: _Ctx, worktree: Path, patch: str, task: str) -> str | None:
    """Apply `patch` in `worktree` and commit it as `epic <run>: <task>`. None on success, else why not."""
    link_venv(ctx.repo, worktree)
    ok, detail = apply_patch(patch, worktree)
    if not ok:
        return f"kept patch did not apply: {detail}"
    ok, detail = _git(*_IDENTITY, "-C", str(worktree), "add", "-A")
    if ok:
        ok, detail = _git(
            *_IDENTITY, "-C", str(worktree), "commit", "--allow-empty", "-q", "-m", f"epic {ctx.run_id}: {task}"
        )
    return None if ok else f"kept patch could not be committed: {detail}"


def _land_branch(ctx: _Ctx, *, task: str, patch: str) -> str | None:
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
        _git("-C", str(ctx.repo), "branch", "-D", branch)
        return f"{HARNESS_FAULT_PREFIX} {why}"
    return None


def rescue_task(ctx: _Ctx, item: Mapping[str, Any], *, phase: str) -> dict[str, Any]:
    """Harness checks on a quarantined task's kept patch, one review round, then a gated move to `approved` or `rescue_failed`."""
    task = str(item["id"])
    attempts = item.get("attempts") or []
    # Under work_state store no work file holds an attempt: the runs' task records are the attempts, and the newest one
    # holds the kept patch (the store's copy, when another lane wrote it). Reading only the file, the rescue the chair
    # launched from those records would find nothing, and the chair would launch it again.
    records = _caused_records(ctx, phase, task) if not attempts and _store_authoritative(ctx) else []
    if records:
        item = {**item, "attempts": [attempt for attempt, _ in records]}
        saved: Mapping[str, Any] | None = records[-1][1]
    else:
        saved = load_result(ctx.runs_dir, attempts[-1]["run"], phase, task) if attempts else None
    patch = rescue_select.patch_of(saved)
    caused = with_stored_causes(item, _stored_causes(ctx, task))
    ok, why = rescue_select.eligible(with_stored_rescues(caused, _stored_rescues(ctx, task)), patch)
    if not ok or patch is None:
        return {"status": "not_eligible", "why": why}
    if not item.get("path") and not _store_authoritative(ctx):
        # With nothing to write in code, the workstore arm would fall back to a model call.
        return {"status": "not_eligible", "why": "no work file to move and no authoritative store"}

    def apply(worktree: Path, text: str) -> str | None:
        link_venv(ctx.repo, worktree)  # as `_build_task` does, so the checks find the same interpreter
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
            env=_checks_env(ctx, {}, phase, f"@rescue-{task}"),
        )
    finally:
        # A rescue is one task under a run id of its own, so the run's whole temp directory goes with it.
        shutil.rmtree(_run_scratch(ctx), ignore_errors=True)
    if not verified["passed"]:
        return _rescue_failed(ctx, item, phase=phase, reason=str(verified["reason"]), failed_checks=True)

    rows = verified["rows"]
    evidence = [*((saved or {}).get("evidence") or []), *rows]
    _save_result(
        ctx,
        {**_without_cause(saved or {}), "initiative": ctx.initiative_id, "phase": phase, "evidence": evidence, **_body_stamp(item)},
        phase=phase,
        task=task,
    )
    graph = rescue_review.run(
        {
            "run_id": f"{ctx.run_id}:{phase}:{task}",
            "date": ctx.date,
            "ticket": task,
            "ticket_title": item.get("title") or "",
            "ticket_body": item.get("body") or "",
            "cartridge": ctx.cartridge,
            "surfaces": list(item.get("surfaces") or []),
            "patterns": list(item.get("patterns") or []),
            "patch": patch,
            "evidence": rows,
        },
        ctx.runner,
    )
    if graph.get("failed_node") or (graph.get("fix_loop") or {}).get("review_placeholder"):
        return {"status": "review_failed", "why": str(graph.get("failed_node_reason") or "review placeholders")}
    if graph.get("verdict") != "approve":
        rationale = str((graph.get("review") or {}).get("rationale") or "no rationale given")
        return _rescue_failed(ctx, item, phase=phase, reason=f"rescue review revised: {rationale}", failed_checks=False)

    # The branch precedes the gated move, so a refused move leaves it in place for the chair to land.
    land_fault = _land_branch(ctx, task=task, patch=patch)
    if land_fault is not None:
        return _rescue_failed(ctx, item, phase=phase, reason=land_fault, failed_checks=False)
    _save_result(
        ctx, {**graph, "initiative": ctx.initiative_id, "phase": phase, "evidence": evidence, **_body_stamp(item)}, phase=phase, task=task
    )
    applied, detail = _rescue_move(
        ctx,
        item,
        phase=phase,
        evidence=[
            {"check": "review_charter verdict", "output": str((graph.get("review") or {}).get("verdict"))},
            *({"check": row["command"], "output": row["output"]} for row in rows),
        ],
    )
    if not applied:
        _record_tasks(ctx, phase, [], [{"id": task, "grain": "task"}])
        return {"status": "move_refused", "why": detail}
    _record_tasks(ctx, phase, [{"id": task, "status": "approved", "state": "approved"}], [])
    return {"status": "approved", "detail": detail}
