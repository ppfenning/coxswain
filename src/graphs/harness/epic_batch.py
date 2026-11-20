"""The epic driver, after a phase: trimming it, its status, the batch of proposals and the branch facts."""

from __future__ import annotations

import contextlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, cast

from core.jsontypes import is_dict, is_mapping
from core.workstore import WorkStoreError, record_attempt
from graphs._contract import proposal
from harness import termination
from harness.cause_model import cause_evidence, classify_with_model, one_line
from harness.cause_rule import classify_cause
from harness.checks import all_passed, coverage_floor_holds, run_checks
from harness.epic_branches import DRAFT_KINDS, IDENTITY, STYLE_PASS_PROMPT, STYLE_PASS_SCHEMA, checks_env
from harness.epic_build import body_stamp, collected_test_ids
from harness.epic_context import Ctx, git_in
from harness.epic_phase_branch import branch_ahead, branch_exists, open_in_phase
from harness.epic_records import (
    END_WRITE_WINDOW_S,
    Execution,
    epic_log,
    fence_reason,
    is_stale_epoch,
    quarantine_row,
    save_task_result,
    utc_now,
    wait_if_paused_here,
)
from harness.epic_types import PhaseRecord, Quarantine, TaskEntry, as_list, as_mapping
from harness.escalate import escalate_self_modification
from harness.resume import load_result
from harness.store_write import Store
from harness.worktree import apply_patch, create_worktree, remove_worktree
from runner.protocol import LimitStop, NodeRunner


def item_path(item: Mapping[str, object] | None) -> str | None:
    """The work file path of `item`, if it has one."""
    path = (item or {}).get("path")
    return path if isinstance(path, str) else None


def quarantine_task(
    ctx: Ctx,
    by_id: Mapping[str, Mapping[str, object]],
    *,
    phase: str,
    task: str,
    reason: str,
    kind: Literal["refused", "no_work", "unverified", "infra", "rescue_failed", "stale_base"],
    detail: str | None = None,
    result: Mapping[str, object] | None = None,
    cause: tuple[str, str] | None = None,
    approved_is_open: bool = False,
) -> Quarantine:
    """Build a task's quarantine entry AND leave a record on its own work item.

    The phase record is the run's memory; the item file is the task's own, so
    the next run — which may not even resume this one — still sees why the
    last attempt didn't land. A task built with no file behind it (as tests
    do) or a store that refuses the write loses the item-side memory, never
    the quarantine entry itself.

    `kind` is the closed set from docs/design/observed-record.md §3. `infra`
    reaches here three ways: the caller's own branch at the `open_phase_worktree`
    failure site, which marks the phase `phase_failed_to_start` before this
    function ever sees a task; `execute_cleared`, when the apply arm itself raises
    rather than reports; and a non-build node raising mid-round, where the patch
    a later node was handed is kept rather than discarded. `unverified` is an
    approved patch the validator would not sign off, kept for the same reason.
    Both carry `patch_kept: True` on both the entry and the attempt. `infra` from
    `execute_cleared` names the BUILD's already-approved patch already on the task
    record, not a resumable arm session: `auto_apply`'s `runner.run` call carries
    no `thread=`, so nothing about the failed arm call itself survives to be
    resumed.

    `stale_base` is an empty build whose base lacks a file the ticket names; it keeps no patch.

    `detail`, when given, is stored as the attempt's own `reason` in place of
    the terse one on `entry` — the next build's carried-forward brief gets the
    fuller text, the printed quarantine line stays short.

    With a store, the attempt and the task record also carry a cause: the
    deterministic rule first, the cheap classifier only when it has no answer.
    `result`, the fix loop's own record of a refused build, supplies the
    classifier's arbitration text; without it the quarantine reason is the
    text. A classifier failure records `unknown` and never blocks the
    quarantine. The session limit is not a failure of the task: it propagates
    before anything is written, so the run pauses with no attempt recorded.

    A failure seen while the run is terminating is the operator's kill, not the task's: nothing is recorded and the task stays ready.

    `cause`, a (cause, cause_why) the caller already knows, replaces that judgment.
    `rescue_task` passes it for `rescue_failed`, whose cause a rule fixed.

    `approved_is_open` lets the store row leave `approved`: the end-of-run audit quarantines a task already moved there.
    """
    stale = fence_reason(ctx)
    if stale is not None:
        # A stale leader records no attempt anywhere, so it is not an attempt either.
        return {"id": task, "phase": phase, "grain": "task", "reason": f"{stale}; not recorded: {reason}", "kind": "no_work"}
    if termination.is_terminating():
        # Wrong belief: a failure seen after SIGTERM is a failed build. It is the kill, so it is no attempt and the task stays ready.
        return {"id": task, "phase": phase, "grain": "task", "reason": f"run terminated by signal; not recorded: {reason}", "kind": "no_work"}
    # Before any write, so a `LimitStop` from the classifier leaves nothing half recorded.
    judged = (cause or _cause_of(ctx.runner, kind, reason, result, task)) if ctx.store is not None else None
    patch_kept = kind in ("unverified", "infra")
    entry: Quarantine = {"id": task, "phase": phase, "grain": "task", "reason": reason, "kind": kind}
    if patch_kept:
        entry["patch_kept"] = True
    ts = utc_now()
    path = item_path(by_id.get(task))
    if path:
        with contextlib.suppress(WorkStoreError, OSError):
            record_attempt(
                path,
                run=ctx.run_id,
                phase=phase,
                reason=detail or reason,
                kind=kind,
                ts=ts,
                **({"patch_kept": True} if patch_kept else {}),
            )
    if ctx.store is not None and judged is not None:
        # The store's attempt exists whether or not the item has a file behind it.
        judged_cause, cause_why = judged
        store = ctx.store

        def attempt() -> None:
            # The count and the insert retry as one unit, so a retry recounts rather than reusing a stale seq.
            seq = _next_attempt_seq(store, ctx.run_id, task)
            store.record_attempt(
                ctx.run_id, task, seq, phase, kind, detail or reason, ts, epoch=ctx.epoch, cause=judged_cause, cause_why=cause_why
            )

        ctx.retry.write(attempt, END_WRITE_WINDOW_S)
        record_task_cause(ctx, phase, task, judged_cause, cause_why, by_id.get(task))
    # A merge conflict leaves the row at approved with its work unlanded; approved is not always finished.
    quarantine_row(ctx, phase, task, approved_is_open=approved_is_open or judged == ("harness", "merge conflict"))
    return entry


def _cause_of(runner: NodeRunner, kind: str, reason: str, result: Mapping[str, object] | None, task: str) -> tuple[str, str]:
    """(cause, why): the rule first, then the model. Only `LimitStop` escapes: the account's limit is not the task's cause."""
    ruled = classify_cause(kind, reason)
    if ruled is not None:
        return ruled, f"rule: kind {kind}"
    reasoning, claims = cause_evidence(reason, result)
    try:
        return classify_with_model(runner, reasoning, claims, task=task)
    except LimitStop:
        raise
    except Exception as exc:
        return "unknown", one_line(f"classifier failed: {type(exc).__name__}: {exc}")


def record_task_cause(
    ctx: Ctx, phase: str, task: str, cause: str, cause_why: str, item: Mapping[str, object] | None = None,
) -> None:
    """Put the cause, and the body the task was built on (`body_stamp(item)`), on this run's saved result, which
    mirrors it into the store row: file and row stay equal.

    A task with no saved result gets a minimal result-shaped one. A failure is a warning, never a block.
    """
    if ctx.store is None:
        return
    try:
        saved = load_result(ctx.runs_dir, ctx.run_id, phase, task) or {
            "ticket": task,
            "initiative": ctx.initiative_id,
            "phase": phase,
        }
        save_task_result(ctx, {**saved, "cause": cause, "cause_why": cause_why, **body_stamp(item)}, phase=phase, task=task)
    except Exception as exc:
        epic_log.warning("cause for %s/%s/%s not recorded on the task record: %r", ctx.run_id, phase, task, exc)


def _next_attempt_seq(store: Store, run_id: str, task: str) -> int:
    """The attempts already stored for (run, task), so a rerun of the same run id appends rather than collides."""
    mark = store.conn.dialect.placeholder
    row = store.conn.query_one(f"SELECT COUNT(*) FROM attempts WHERE run_id = {mark} AND task_id = {mark}", (run_id, task))
    return 0 if row is None else int(cast(int, row[0]))


INTERRUPTED_KIND = "interrupted"  # a task the lost lease stopped mid-run; never an attempt


def stale_phase(record: PhaseRecord, reason: str) -> PhaseRecord:
    """Stop the phase on a fence, so it neither reads complete nor unblocks its dependents.

    A lost lease leaves the phase `interrupted`. Its in-flight tasks, and the task entries `quarantine_task`
    returned once fenced, move from `quarantined` to `interrupted`. Merged and otherwise quarantined tasks stay.
    """
    record["reason"] = reason
    if "quarantined" not in record or "task_records" not in record:
        raise KeyError("a phase record with no quarantined or task_records cannot be stopped on a fence")
    if not is_stale_epoch(reason):
        record["status"] = "blocked"
        record["quarantined"].append(
            {"id": record["phase"], "phase": record["phase"], "grain": "phase", "reason": reason, "kind": "no_work"}
        )
        return record
    lost = {q["id"]: q["reason"] for q in record["quarantined"] if _fenced_task_entry(q)}
    record["quarantined"][:] = [q for q in record["quarantined"] if not _fenced_task_entry(q)]
    in_flight = [
        t["id"]
        for t in record["task_records"]
        if t["id"] in lost or not (t.get("merged") or t.get("quarantine") or t.get("status") == "quarantined")
    ]
    record["status"] = "interrupted"
    record["interrupted"] = [
        {"id": i, "phase": record["phase"], "grain": "task", "reason": lost.get(i, reason), "kind": INTERRUPTED_KIND}
        for i in [*in_flight, *(i for i in lost if i not in in_flight)]
    ]
    if "totals" in record:
        record["totals"] = {**record["totals"], "quarantined": record["totals"]["quarantined"] - len(lost)}
    return record


def _fenced_task_entry(entry: Mapping[str, object]) -> bool:
    """A task quarantine entry that `quarantine_task` returned because the lease was already lost."""
    return entry.get("grain") == "task" and is_stale_epoch(str(entry.get("reason")))


def trim_phase(ctx: Ctx, phase: str) -> str | None:
    """Run `style_pass` once over the phase's assembled diff, gated on the coverage floor.

    §5 is literal: the diff is against `main` (`ctx.default_ref`), not the
    parent phase's branch — a phase branch is stacked on its unlanded parent
    within one run, so diffing anything narrower would hide exactly the
    cross-phase duplication and shims the extra sentence exists to catch.

    `None` means style_pass had nothing to offer — an empty patch is not a
    refusal, there was no trim to gate. Otherwise the outcome half of the
    phase record's `trim: ...` line (docs/design/landing-model.md §5). Before
    and after both collect from fresh worktrees of the phase branch, never
    `ctx.phase_worktree(phase)` itself — it still holds every task's own
    build worktree nested under it, and `pytest --collect-only` would walk
    straight into them. Every refusal path returns before the real phase
    worktree is touched, so a tripped floor leaves the branch exactly as
    `validate_phase` left it. Both scratch worktrees and the branches they
    were created on are torn down on every exit from the `with`, whether the
    floor held or not — `create_worktree` names them, nobody else deletes them.
    """
    worktree = ctx.phase_worktree(phase)
    branch = ctx.phase_branch(phase)
    ok, diff = git_in("-C", str(ctx.repo), "diff", f"{ctx.default_ref}...{branch}")
    if not ok or not diff.strip():
        return None
    # The only direct `ctx.runner.run` call this module owns. No `task=` here:
    # `style_pass` runs once over the whole PHASE diff — every task's work
    # combined — so there is no single task id to stamp, and its ledger row
    # is `role: "style_pass"`, which `trace_evidence`'s `role == "build"`
    # filter excludes regardless of `task_id` (see the test naming this call).
    result = ctx.runner.run(
        role="style_pass",
        tier="standard",
        schema=STYLE_PASS_SCHEMA,
        wait_if_paused=lambda: wait_if_paused_here(ctx),
        prompt=f"{STYLE_PASS_PROMPT}\n\nPhase: {phase}\n\nPhase diff:\n{diff}",
        context=as_list(ctx.cartridge.get("context")),
    )
    patch = str(result.get("patch") or "")
    if not patch.strip():
        return None

    before_branch = f"epic-trim-before/{ctx.run_id}/{phase}"
    after_branch = f"epic-trim-after/{ctx.run_id}/{phase}"
    outcome: str | None = None
    with TemporaryDirectory() as scratch:
        before_tree = Path(scratch) / "before"
        after_tree = Path(scratch) / "after"
        try:
            ok, _ = create_worktree(ctx.repo, before_tree, branch=before_branch, base=branch)
            before = collected_test_ids(before_tree) if ok else None

            ok, _ = create_worktree(ctx.repo, after_tree, branch=after_branch, base=branch)
            applied = ok and apply_patch(patch, after_tree)[0]
            after = collected_test_ids(after_tree) if applied else None

            if before is None or after is None:
                outcome = "refused (coverage floor)"
            else:
                checks_ok = all_passed(run_checks(after_tree, ctx.checks, env=checks_env(ctx, {}, phase, "@trim"))) if ctx.checks else True
                if not (coverage_floor_holds(before, after) and checks_ok):
                    outcome = "refused (coverage floor)"
        finally:
            remove_worktree(ctx.repo, before_tree)
            remove_worktree(ctx.repo, after_tree)
            git_in("-C", str(ctx.repo), "branch", "-D", before_branch)
            git_in("-C", str(ctx.repo), "branch", "-D", after_branch)
    if outcome is not None:
        return outcome

    applied, _ = apply_patch(patch, worktree)
    if not applied:
        return "refused (coverage floor)"
    # `-u`, never `-A`: `worktree` still holds every task's own nested build
    # worktree (`task_worktree(phase, task) = phase_worktree(phase) / task`),
    # untracked from the phase branch's own point of view. `-A` would stage
    # each one as a gitlink at mode 160000, landing a pointer at a commit on
    # the harness-owned `agents/<run>/<task>` scratch namespace into the
    # commit this phase later squash-merges to main. `-u` restages only paths
    # already tracked on the branch — exactly what style_pass's patch touched.
    ok, _ = git_in(*IDENTITY, "-C", str(worktree), "add", "-u")
    if ok:
        ok, _ = git_in(*IDENTITY, "-C", str(worktree), "commit", "-q", "-m", f"epic {ctx.run_id}: trim {phase}")
    if not ok:
        return "refused (coverage floor)"
    removed = sum(1 for line in patch.splitlines() if line.startswith("-") and not line.startswith("---"))
    return f"{removed} lines removed"


def phase_status(
    verdict: Mapping[str, object] | None,
    *,
    validated: bool,
    ready: Sequence[Mapping[str, object]],
    quarantined: Sequence[Mapping[str, object]],
    items: Sequence[Mapping[str, object]],
    phase: str,
    landed: bool,
    branch: str,
    land_at: str,
) -> tuple[str, str]:
    """Complete, or partial — and a phase unblocks its dependents only when complete.

    Blanket no, decided. A partially complete phase turns one quarantined task
    into a phase of work built on ground that is not there; the refinement
    (`quarantine_blocks_dependents`) is what the validator reports, and acting
    on it is a heavier ask of that role than anything else in the spec.

    An unbound `validate_phase` is not an approval. A team that binds no
    validator gets task completion and NO claim about phase completion, which
    is honest — so the phase is partial, and its dependents wait.

    A met goal is not sufficient either, and this is where open question 1 gets
    its answer: the verdict is about work that exists on branches, and the gate
    decides whether that work reaches the phase branch. If the merges were
    refused, the next phase would branch from a phase branch with nothing on it,
    so the phase is partial no matter how good the work was.
    """
    if not ready and not quarantined and not open_in_phase(items, phase, (), land_at):
        return "complete", "every task in the phase was already done"
    if any(item.get("phase") == phase and item.get("state") == "quarantined" for item in items):
        return "partial", "a task in the phase is quarantined"
    if not validated:
        return "partial", "no validator bound"
    if verdict is None:
        return "partial", "the validator produced no verdict"
    if not verdict.get("goal_met"):
        return "partial", str(verdict.get("reasoning") or "the phase goal was not met")
    if not landed:
        return "partial", f"the goal is met, but nothing was merged into {branch}"
    return "complete", ""


def _finding_entries(result: Mapping[str, object], chunk: Mapping[str, object]) -> list[tuple[str, str]]:
    """Every (text, file) a review, adversary or validate verdict wrote for one task.

    Only a review finding's `file` is structured: REVIEW_SCHEMA requires it on
    every entry (`graphs/delivery/lifecycle_propose.py`), so it is read straight
    off rather than guessed at. An adversary objection and a validate gap or
    reasoning string carry no such field — `("text", "")` — and are matched on
    their own prose in `_cited_surface` below.
    """
    review = [as_mapping(f) for f in as_list(as_mapping(result.get("review")).get("findings"))]
    adversary = [as_mapping(o) for o in as_list(as_mapping(result.get("adversary")).get("objections"))]
    entries = [(str(f.get("detail") or ""), str(f.get("file") or "")) for f in review]
    entries += [(f"{o.get('claim') or ''} {o.get('why_wrong') or ''}".strip(), "") for o in adversary]
    entries += [(str(g), "") for g in as_list(chunk.get("gaps"))]
    entries.append((str(chunk.get("reasoning") or ""), ""))
    return entries


def _cited_surface(text: str, file: str, surfaces: Sequence[str]) -> str | None:
    """The one entry of `surfaces` that `file` names outright, or that `text` quotes."""
    if file and file in surfaces:
        return file
    return next((s for s in surfaces if s and s in text), None)


def _consolidation_pairs(
    built: Mapping[str, Mapping[str, object]],
    surviving: Sequence[str],
    chunk_by_task: Mapping[str, Mapping[str, object]],
    by_id: Mapping[str, Mapping[str, object]],
) -> list[dict[str, str]]:
    """One entry per pair of surviving tasks a finding names as blocked on a file the other owns.

    `docs/design/work-shape.md` §4, quoted verbatim: "a review, adversary or
    validate finding stating that a build cannot satisfy its ticket without
    touching a file another ticket in the phase owns." §4 names no phrase to
    match — the fact it describes is OWNERSHIP, and `docs/design/work-shape.md`
    §3's coupling rule already computes that off each ticket's own `surfaces`
    list, so a finding qualifies here on the same evidence: the file it names
    is a `surfaces` entry of a DIFFERENT surviving task in this phase, never on
    a model's choice of words.
    """
    seen: set[frozenset[str]] = set()
    pairs: list[dict[str, str]] = []
    for task in surviving:
        entries = _finding_entries(as_mapping(built[task]["result"]), chunk_by_task.get(task) or {})
        for other in surviving:
            if other == task or frozenset({task, other}) in seen:
                continue
            surfaces = [str(s) for s in as_list((by_id.get(other) or {}).get("surfaces"))]
            if not surfaces:
                continue
            for text, file in entries:
                cited = _cited_surface(text, file, surfaces)
                if cited is None:
                    continue
                seen.add(frozenset({task, other}))
                pairs.append({"task": task, "other": other, "file": cited, "text": text})
                break
    return pairs


def state_move_apply(path: str | None, state: str) -> dict[str, object]:
    """The `apply` payload the code arm reads; empty when no path is known, so the gate falls back to the model arm."""
    return {"apply": {"path": str(path), "state": state}} if path else {}


def item_apply(path: str | None, item: Mapping[str, object]) -> dict[str, object]:
    """The `apply` payload for item_create and item_update; empty when no path is known."""
    return {"apply": {"path": str(path), "item": dict(item)}} if path else {}


def build_batch(
    ctx: Ctx,
    *,
    phase: str,
    surviving: Sequence[str],
    built: Mapping[str, Mapping[str, object]],
    escalated: set[str],
    chunk_by_task: Mapping[str, Mapping[str, object]],
    rebase: Mapping[str, object] | None,
    by_id: Mapping[str, Mapping[str, object]],
) -> tuple[list[dict[str, object]], dict[int, tuple[str, str]]]:
    """Everything this phase asks for, in task-id order, plus what each slot means.

    The slot map is by identity rather than by target string, because the same
    proposal objects travel through `split_by_policy` and `gate` unchanged and
    the driver has to know, at execution time, which branch a given proposal
    was about. Reparsing a target would be a second encoding of the same fact.
    """
    batch: list[dict[str, object]] = []
    slots: dict[int, tuple[str, str]] = {}

    for task in surviving:
        for item in as_list(built[task]["proposals"]):
            if not is_dict(item):
                continue
            batch.append(item)
            if item.get("kind") in DRAFT_KINDS:
                slots[id(item)] = ("draft", task)

    for task in surviving:
        merge = proposal(
            ctx.cartridge,
            kind="merge_stack",
            target=f"{ctx.draft_branch(phase, task)} -> {ctx.phase_branch(phase)}",
            evidence=[
                *[e for e in as_list(built[task]["evidence"]) if is_mapping(e)],
                {
                    "check": "validate_chunk",
                    "output": (
                        f"satisfied — {chunk_by_task[task].get('reasoning')}"
                        if task in chunk_by_task
                        else "not bound; no chunk verdict was produced"
                    ),
                },
            ],
            rationale=f"{task} is reviewed, applied and checked on its own branch off {ctx.phase_branch(phase)}",
            suggested_action=f"merge {ctx.draft_branch(phase, task)} into {ctx.phase_branch(phase)} (no fast-forward)",
        )
        if task in escalated:
            # The patch touched governance, so the MERGE of that patch is a
            # governance write too. Escalating it here is what makes "its merge
            # is impossible to earn" true rather than merely intended.
            merged, _ = escalate_self_modification(
                [merge],
                patch=str(as_mapping(as_mapping(built[task]["result"]).get("build")).get("patch") or ""),
                cartridge=ctx.cartridge,
                ledger_path=ctx.ledger_path,
            )
            merge = merged[0]
        batch.append(merge)
        slots[id(merge)] = ("merge", task)

    if rebase is not None:
        batch.append(dict(rebase))
        slots[id(batch[-1])] = ("rebase", phase)

    for task in surviving:
        move = proposal(
            ctx.cartridge,
            kind="state_move",
            target=task,
            evidence=[
                {
                    "check": "review_charter verdict",
                    "output": str(as_mapping(as_mapping(built[task]["result"]).get("review")).get("verdict")),
                },
                *[e for e in as_list(built[task]["evidence"]) if is_mapping(e)],
            ],
            rationale=f"{task} was built, checked and reviewed in this run",
            # `approved`, never `done`: the apply arm writes the state this names, and only
            # `cox runs land` writes `done`, after the merge (tools #153).
            suggested_action=f"mark {task} approved",
        )
        move = {**move, **state_move_apply(item_path(by_id.get(task)), "approved")}
        batch.append(move)
        slots[id(move)] = ("state_move", task)

    for pair in _consolidation_pairs(built, surviving, chunk_by_task, by_id):
        task, other, file, text = pair["task"], pair["other"], pair["file"], pair["text"]
        ids = " and ".join(sorted((task, other)))
        # Propose only, per conventions.md's propose-don't-write posture: no
        # slots entry, so `execute_cleared`'s default ("other", "") slot never merges
        # or moves anything on this proposal's account.
        batch.append(
            proposal(
                ctx.cartridge,
                kind="consolidate",
                target=ids,
                evidence=[{"check": "review/adversary/validate finding", "output": text}],
                rationale=f"{task} cannot satisfy its ticket without touching {file}, which {other} owns",
                suggested_action=f"merge {ids} into one ticket and mark the superseded one dropped",
            )
        )

    return batch, slots


def kept_work_refusal(landed: bool, merged: bool | None, escalated: bool, ahead: bool) -> str | None:
    """None when the task's work is on a branch that survives the run, else which part of that failed.

    An escalated task never merges: its draft branch is the kept work. `merged` is None when no merge ran.
    """
    if escalated:
        if not landed:
            return "the draft branch was not created"
        if not ahead:
            return "the draft branch has no commits the default ref lacks"
        return None
    if not landed:
        return "the draft did not land"
    if merged is None:
        return "the merge into the phase branch was not attempted"
    if not merged:
        return "the merge into the phase branch was refused or conflicted"
    if not ahead:
        return "the merge added nothing to the phase branch"
    return None


def unkept_work(ctx: Ctx, phase: str, subject: str, state: Execution) -> str | None:
    """Read what happened to the task's draft and merge, and report why its work is not kept, if it is not."""
    escalated = subject in state.escalated
    branch = ctx.scratch_branch(subject) if escalated else ctx.phase_branch(phase)
    return kept_work_refusal(
        landed=branch_exists(ctx, branch) if escalated else bool(state.landed.get(subject)),
        merged=state.merged.get(subject),
        escalated=escalated,
        ahead=branch_ahead(ctx, branch),
    )


def draft_skipped_note(*, landed: bool, merged: bool | None, escalated: bool, branch_kept: bool, detail: str) -> str | None:
    """The note for an approved task whose draft did not land but whose scratch branch holds its work, else None.

    A merge that ran and failed is not this case: that task is quarantined by its merge. `detail` is why the draft is absent.
    """
    if escalated or landed or merged is not None or not branch_kept:
        return None
    return f"approved without a draft branch: {detail}. The work is on the task branch; `runs land` creates the PR."


def draft_note(ctx: Ctx, subject: str, state: Execution) -> str | None:
    """Read the scratch branch and the draft step's outcome, and return the note when the work is kept without a draft."""
    scratch = ctx.scratch_branch(subject)
    return draft_skipped_note(
        landed=bool(state.landed.get(subject)),
        merged=state.merged.get(subject),
        escalated=subject in state.escalated,
        branch_kept=kept_work_refusal(
            landed=branch_exists(ctx, scratch), merged=None, escalated=True, ahead=branch_ahead(ctx, scratch)
        )
        is None,
        detail=state.draft_detail.get(subject) or "the draft step did not run or was not approved",
    )


@dataclass(frozen=True)
class BranchFacts:
    """What git says at the end of a run about the branch an approved task's work should be on.

    `holds_merge` is whether the task's draft tip is an ancestor of the phase branch; True for a task that never merged.
    """

    branch: str
    draft: str | None
    exists: bool
    holds_merge: bool
    ahead: bool


def end_of_run_refusal(facts: BranchFacts, *, merged: bool) -> str | None:
    """None when an approved task's work is still on its branch, else why not.

    A merged task's phase branch must exist and hold its draft's tip: being ahead of the default ref is not enough,
    since a recreated or stacked phase branch is ahead on a sibling's or a parent's commits.
    A task that never merged is the escalated one: its draft branch is the kept work, not the phase branch.
    """
    if not facts.exists:
        return f"branch {facts.branch} no longer exists"
    if merged and not facts.holds_merge:
        return f"branch {facts.branch} no longer contains the merge of {facts.draft}"
    if merged:
        return kept_work_refusal(landed=True, merged=True, escalated=False, ahead=facts.ahead)
    return kept_work_refusal(landed=True, merged=None, escalated=True, ahead=facts.ahead)


def _holds(ctx: Ctx, commit_branch: str | None, branch: str) -> bool:
    """True when `commit_branch`'s tip is an ancestor of `branch`. A missing branch on either side is False."""
    if not commit_branch:
        return False
    ok, _ = git_in(
        "-C", str(ctx.repo), "merge-base", "--is-ancestor", f"refs/heads/{commit_branch}", f"refs/heads/{branch}"
    )
    return ok


def _gather_branch_facts(ctx: Ctx, records: Sequence[Mapping[str, object]]) -> dict[str, BranchFacts]:
    """Per task id, the git facts `end_of_run_refusal` judges. The only part of the audit that reads git."""
    facts: dict[str, BranchFacts] = {}
    for record in records:
        task = str(record["id"])
        merged = bool(record.get("merged"))
        recorded = record.get("draft")
        draft = recorded if isinstance(recorded, str) else None
        branch = ctx.phase_branch(str(record["phase"])) if merged else ctx.scratch_branch(task)
        facts[task] = BranchFacts(
            branch=branch,
            draft=draft,
            exists=branch_exists(ctx, branch),
            holds_merge=_holds(ctx, draft, branch) if merged else True,
            ahead=branch_ahead(ctx, branch),
        )
    return facts


def audit_approved_kept(
    ctx: Ctx, by_id: Mapping[str, Mapping[str, object]], tasks: Sequence[TaskEntry], quarantined: list[Quarantine]
) -> None:
    """Quarantine each task this run moved to approved whose work is no longer on a branch. Never recovers it."""
    approved = [t for t in tasks if t.get("state") == "approved" and not t.get("quarantine")]
    facts = _gather_branch_facts(ctx, approved)
    for record in approved:
        why = end_of_run_refusal(facts[str(record["id"])], merged=bool(record.get("merged")))
        if why is None:
            continue
        # `no_work`, not `infra`: `infra` marks a patch as kept, and the branch that kept it is what went missing.
        entry = quarantine_task(
            ctx,
            by_id,
            phase=str(record["phase"]),
            task=str(record["id"]),
            reason=f"work not on a surviving branch at the end of the run: {why}",
            kind="no_work",
            cause=("harness", "work not on a surviving branch"),
            approved_is_open=True,
        )
        quarantined.append(entry)
        record.update(
            status="quarantined", state="quarantined", quarantine=entry["reason"], reason=entry["reason"],
            outcome="harness_fault",
        )
