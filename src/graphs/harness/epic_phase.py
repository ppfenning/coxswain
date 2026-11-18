"""The epic driver, one phase: `run_phase_tasks`, from the fan-out to the gate."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict, cast

from core import ledger, workstore
from core.landing import is_initiative, land_gate_level
from core.landing_record import INTEGRATE_PHASE
from core.manifest import append_ledger, build_manifest, gate_diff
from core.phase_hold import phase_holds
from graphs._contract import proposal
from graphs.delivery.repo_layout import repo_key
from harness import rescue_select
from harness.base_check import stale_base_stop
from harness.base_sync import fetch_origin, merge_in
from harness.cause_rule import is_auth_failure
from harness.check_gate import gate_commands
from harness.checks import HARNESS_FAULT_PREFIX, check_feedback, is_harness_fault
from harness.digest import build_digest
from harness.epic_batch import (
    build_batch,
    phase_status,
    quarantine_task,
    record_task_cause,
    stale_phase,
    trim_phase,
)
from harness.epic_branches import (
    DRAFT_KINDS,
    PATCH_FOR_VALIDATION_CHARS,
    TRIAGE,
    VALIDATE,
    PhaseRunner,
    attempt_tree,
    carry_forward,
    fetch_or_stop,
    invoke_from_attempt,
    missing_branch_phase,
    open_grain_pr,
    opens_pr,
    parent_gap,
    phase_branches_to_fetch,
    previous_objection_block,
    resume_body,
)
from harness.epic_build import (
    body_stamp,
    build_task,
    lifecycle_invocation,
    refix_task,
    run_level_checks,
    run_level_integration,
    task_outcome,
    unapproved,
    verify_of,
)
from harness.epic_context import Ctx, git_in, phase_goal, read_evidence_file
from harness.epic_execute import (
    caused_records,
    execute_cleared,
    policy_split,
    record_attempts,
    records_after,
    stored_causes,
    unreadable_reason,
    with_record_attempts,
    with_stored_causes,
    without_cause,
)
from harness.epic_phase_branch import (
    ASSEMBLY_FAILED,
    blocking_commits,
    branch_action,
    classify_unlanded,
    locked_merges,
    needs_missing,
    needs_other_phase,
    note_push_failure,
    open_in_phase,
    open_phase_worktree,
    phase_branch_cherry,
    publish_branch,
    rebase_phase_branch,
    recorded_merges,
    refresh_level,
    required_refs,
    start_failure,
    waiting_tasks,
)
from harness.epic_records import (
    END_WRITE_WINDOW_S,
    PRINCIPAL,
    Execution,
    default_branch_commit,
    default_branch_paths,
    epic_log,
    fence_reason,
    git_stdout,
    heal_surfaces,
    quarantine_row,
    read_store_rows,
    record_tasks,
    save_task_result,
    store_authoritative,
    store_fresh,
    surface_refusal_reason,
    wait_if_paused_here,
    without_correction_lint,
    working_tree_paths,
    write_healed_surfaces,
)
from harness.epic_triage import (
    amend_in_store,
    amended_body,
    apply_amend_directly,
    attempt_cap_for,
    attempt_cap_reason,
    base_expected,
    base_gaps,
    build_attempts,
    buildable,
    combine_task,
    drop_stale_triage_flag,
    drop_task,
    earlier_merged,
    hold_task,
    invoke_failure_kind,
    is_append_only,
    newest_attempt_text,
    quarantine_missing_merges,
    read_queue_rows,
    record_triage,
    row_extra,
    stale_base_missing,
    ticket_amend_ramp,
    triage_failure_hold,
    triage_initiative_context,
)
from harness.epic_types import PhaseRecord, Quarantine, as_list, as_mapping
from harness.escalate import escalate_self_modification
from harness.gate import apply_arm_for, gate
from harness.handoff import plan_context
from harness.invoke import Invocation, invoke_graphs
from harness.resume import load_result, reusable
from harness.stale_base import is_patch_empty, stale_base_reason, wrong_repository, wrong_repository_reason
from harness.surface_heal import malformed_surfaces, notes_out_of_surfaces
from harness.ticket_check import check_ticket, ticket_lookups
from harness.worktree import registry_lock


class _TaskExtras(TypedDict, total=False):
    """The keys a task entry carries only when the build produced them."""

    test_database: str
    refix: object


def run_phase_tasks(
    ctx: Ctx,
    *,
    initiative: Mapping[str, Any],
    phase: str,
    parents: tuple[str, ...],
    items: list[dict[str, Any]],
    only: frozenset[str] | None = None,
) -> PhaseRecord:
    """One phase: branch, fan out, check, validate, gate, execute, record.

    One parent stacks the branch on that parent's branch. Several parents are merged by
    `assemble` onto the default ref, so `base` is the default ref and `assembled_from` lists them.
    At land_at ticket, once the pr branch exists, a phase that needs an earlier phase is cut from it and assembles nothing.
    With `only`, just those ready tasks build; the validator, once the phase is finished, sees the whole phase.
    """
    branch = ctx.phase_branch(phase)
    parent = parents[0] if len(parents) == 1 else None
    # A land marks a phase's tasks done, and `phase_parents` then drops the edge, so the pr base is chosen on
    # the needs themselves: a phase that needs a task of another phase builds on the pr branch, done or not.
    with ctx.items_lock:
        stacked = bool(parents) or needs_other_phase(items, phase)
    # At initiative every phase is cut from the initiative branch, whose tip holds every merged parent phase.
    # (`stacked` still names the phases that need another phase's work, for a run with no initiative branch.)
    pr_ref = ctx.pr_branch_ref() if is_initiative(ctx.land_at) and (stacked or ctx.initiative_branch) else None
    base_ref = pr_ref or (ctx.phase_branch(parent) if parent else ctx.default_ref)
    quarantined: list[Quarantine] = []
    assembled: PhaseRecord = {"assembled_from": list(parents)} if len(parents) > 1 and not pr_ref else {}
    record: PhaseRecord = {
        "phase": phase,
        "parent": parent,
        **assembled,
        "branch": branch,
        "base": base_ref,
        "land_at": ctx.land_at,
        "status": "partial",
        "rebased": False,
        "task_records": [],
        "quarantined": quarantined,
        "batch": [],
        "cleared_worktrees": [],
    }

    # A stale leader opens no branch and builds nothing.
    stale_start = fence_reason(ctx)
    if stale_start:
        return stale_phase(record, stale_start)

    # A branch under a parent is cut from the default ref and holds only the commits its tasks need.
    with ctx.items_lock:
        from_pr = pr_ref is not None
        needed = required_refs(items, phase, parents, ctx.phase_branch, base_ref, from_pr=from_pr)
        cut_from = required_refs(items, phase, parents, ctx.phase_branch, base_ref, cut=True, from_pr=from_pr)
    # A lone parent that is settled is the stack itself: cut from it, with no merge commit.
    cut_ref = base_ref if not parents or cut_from == [base_ref] else ctx.default_ref

    def open_failed(detail: str, verb: str) -> PhaseRecord:
        record["status"] = "blocked"
        record["reason"] = start_failure(detail, verb)
        if not detail.startswith(ASSEMBLY_FAILED):
            record["phase_failed_to_start"] = record["reason"]
        if detail.startswith(ASSEMBLY_FAILED) and only:
            # A conflict between needed commits holds the starting tasks only; the phase's other tasks stay as they were.
            with ctx.items_lock:
                by_id = {str(i["id"]): i for i in items}
            quarantined.extend(
                quarantine_task(
                    ctx, by_id, phase=phase, task=tid, reason=record["reason"], kind="infra",
                    cause=("harness", "assembly conflict"),
                )
                for tid in sorted(only)
            )
        else:
            quarantined.append({"id": phase, "phase": phase, "grain": "phase", "reason": record["reason"]})
        return record

    # A branch an earlier phase left on origin from another host becomes a local branch here; one that is
    # nowhere stops the phase before any worktree opens, so assembly only ever fails on a real conflict.
    parent_branches = [] if from_pr else [ctx.phase_branch(p) for p in parents]
    fetch, required, loose = phase_branches_to_fetch(ctx.initiative_id, branch, parent_branches, needed, cut_from)
    if (stopped := fetch_or_stop(ctx, record, fetch, required, loose, parent_branches)) is not None:
        return stopped

    if ctx.initiative_branch is not None:
        # The phase is cut from the initiative branch, so every parent has to be merged into it in this clone first.
        # Nothing is quarantined: the phase starts on a later run, once the parent's merge is here.
        for gap in filter(None, (parent_gap(ctx, p, items) for p in parents)):
            reason, missing = gap
            if missing:
                return missing_branch_phase(record, missing, reason)
            record.update(status="blocked", reason=reason)
            return record
        if ctx.parent_behind and phase != INTEGRATE_PHASE:
            # The branch could not take its parent forward: building now would build on the old base. Nothing is
            # quarantined, and the integrate phase, which resolves a conflict, builds.
            record.update(status="blocked", reason=f"phase {phase} waits: {ctx.parent_behind}")
            return record

    opened = open_phase_worktree(ctx, phase, cut_ref, parents, cut_from)
    ok, detail, branch_reused = opened
    record["cleared_worktrees"] = list(getattr(opened, "cleared", ()))
    if not ok:
        return open_failed(detail, "created")
    record["reused_branch"] = branch_reused
    note_push_failure(record, publish_branch(ctx, branch))

    # Decided before a single task builds: a reused branch missing a commit it
    # needs is either recreated (nothing of its own to lose) or blocked (something is).
    # With an initiative branch none of this applies: the parent does not float, so a reused phase branch is never
    # stale against it. It is behind only by what siblings merged, and `refresh_level` merges that forward below.
    head_moved = ctx.initiative_branch is None and branch_reused and needs_missing(ctx, phase, needed)
    with ctx.items_lock:
        task_states = {str(i["id"]): str(i.get("state")) for i in items}
    cherry = phase_branch_cherry(ctx, phase, pr_ref) if head_moved else []
    unlanded_kind = classify_unlanded(cherry, task_states)
    action = branch_action(branch_reused, head_moved, unlanded_kind not in ("landed", "rebuild"))

    foreign = initiative.get("foreign") or {}
    with ctx.items_lock:
        buildable_now = len(buildable(items, phase, foreign, only, ctx.land_at))
    holds = phase_holds(len(waiting_tasks(cherry)), buildable_now)
    if action == "block" and unlanded_kind == "awaiting_land" and not holds:
        # Approved lands in flight, but a ready task can build: leave the branch and its commits as they are.
        action = "proceed"

    if action == "block" and len(parents) > 1 and not pr_ref:
        # A branch assembled from several parents is re-assembled, never rebased onto the default ref.
        assembled_from = ", ".join(ctx.phase_branch(p) for p in parents)
        record["status"] = "blocked"
        record["reason"] = (
            f"phase branch {branch} was assembled from {assembled_from}, one of which has moved since, and it "
            "carries its own commits; a rebase onto the default ref would drop that parent, so re-assemble it"
        )
        quarantined.append({"id": phase, "phase": phase, "grain": "phase", "reason": record["reason"]})
        git_in("-C", str(ctx.phase_worktree(phase)), "checkout", "--detach", "-q")
        return record

    if action == "block" and unlanded_kind == "awaiting_land":
        # Their lands are in flight: nothing to quarantine and nothing to rebase. Wait.
        record["status"] = "awaiting_land"
        record["awaiting_tasks"] = waiting_tasks(cherry)
        record["reason"] = (
            f"phase branch {branch} is behind {base_ref}; its unlanded commits belong to approved "
            "tasks whose lands are still in flight"
        )
        git_in("-C", str(ctx.phase_worktree(phase)), "checkout", "--detach", "-q")
        return record

    if action == "block" and unlanded_kind == "rebase":
        # Some commits already landed (done), the rest are approved: replay the branch onto the new base.
        with ctx.items_lock:
            recorded = recorded_merges(items, phase)
        rebased_ok, rebase_detail = rebase_phase_branch(ctx, phase, base_ref, recorded)
        if not rebased_ok:
            record["status"] = "blocked"
            record["reason"] = f"rebase conflict: {rebase_detail}"
            quarantined.append({"id": phase, "phase": phase, "grain": "phase", "reason": record["reason"]})
            git_in("-C", str(ctx.phase_worktree(phase)), "checkout", "--detach", "-q")
            return record
        record["rebased"] = True
    elif action == "block":
        # The proposal the quiet path below would have built for the same
        # staleness — filed here, unchanged, so the gate has it to decide on.
        record["batch"].append(
            proposal(
                ctx.cartridge,
                kind="stack_rebase",
                target=branch,
                evidence=[
                    {"check": "merge-base --is-ancestor", "output": f"{base_ref} is NOT an ancestor of {branch}"},
                    {"check": "stacked on", "output": f"{branch} was branched from {base_ref}, whose head has moved"},
                ],
                rationale=(
                    f"'{base_ref}' moved since '{branch}' was created, so this phase — and "
                    "everything stacked above it — is sitting on ground that is no longer there"
                ),
                suggested_action=f"rebase {branch} onto {base_ref}",
            )
        )
        record["status"] = "blocked"
        record["reason"] = (
            f"phase branch {branch} is behind {base_ref} and carries its own commits "
            f"({'; '.join(blocking_commits(cherry, task_states))}); its tasks "
            "would build against a base missing landed work — rebase it through the gate"
        )
        quarantined.append({"id": phase, "phase": phase, "grain": "phase", "reason": record["reason"]})
        # Release it, or the next run cannot open this branch at all.
        git_in("-C", str(ctx.phase_worktree(phase)), "checkout", "--detach", "-q")
        return record

    def recreate() -> PhaseRecord | None:
        """Cut the phase branch again from its base; the record to return when the leader is stale or the reopen fails."""
        stale_recreate = fence_reason(ctx)
        if stale_recreate:
            return stale_phase(record, stale_recreate)
        git_in("-C", str(ctx.phase_worktree(phase)), "checkout", "--detach", "-q")
        with registry_lock(ctx.repo):
            git_in("-C", str(ctx.repo), "worktree", "remove", "--force", str(ctx.phase_worktree(phase)))
        git_in("-C", str(ctx.repo), "branch", "-D", branch)
        reopened = open_phase_worktree(ctx, phase, cut_ref, parents, cut_from)
        ok, detail, branch_reused = reopened
        record["cleared_worktrees"] = [*record["cleared_worktrees"], *getattr(reopened, "cleared", ())]
        if not ok:
            return open_failed(detail, "recreated")
        record["reused_branch"] = branch_reused
        note_push_failure(record, publish_branch(ctx, branch))
        return None

    if action == "recreate":
        early = recreate()
        if early is not None:
            return early
        record["recreated"] = (
            f"phase branch {branch} recreated from {base_ref}: its commits belonged to tasks sent back to ready, which build again"
            if unlanded_kind == "rebuild"
            else f"phase branch {branch} recreated from {base_ref}: its commits already landed there via squash-merge"
        )

    if (early := refresh_level(ctx, record, phase, locked_merges(ctx, items, phase))) is not None:
        return early

    # A runner whose nodes can read the world reads THIS phase's branch — not
    # whatever the repository happens to have checked out. Phase N+1 stacks on
    # N, so a build that read the default branch would be patching ground that
    # is no longer there. Duck-typed: the API runner has no such attribute and
    # nothing to point anywhere.
    # The shared runner is never assigned: from here on this phase's `ctx` carries its own view of it.
    worktree = ctx.phase_worktree(phase)
    ctx = replace(
        ctx,
        shared_runner=ctx.runner,
        runner=PhaseRunner(
            ctx.runner,
            worktree,
            build_digest(worktree) or None,
            [str(c.get("cmd")) for c in ctx.checks if c.get("cmd")],
            gate_commands(ctx.landing_areas),
        ),
    )

    # A task approved and merged in an earlier run is not rebuilt. Its commit must be on this phase's branch,
    # and a task whose commit is gone is quarantined alone: no other task's row or state is touched.
    if (paused := quarantine_missing_merges(ctx, phase, items, quarantined)) is not None:
        record.update(status="blocked", reason=paused)
        return record
    with ctx.items_lock:
        handoffs = {str(i["id"]): i["handoff"] for i in items if i.get("handoff")}

    # A dropped task is terminal like a done one: it never gets rebuilt or
    # re-reviewed, and never blocks the phase behind it.
    with ctx.items_lock:
        all_ready = buildable(items, phase, foreign, only, ctx.land_at)
    # Under work_state store no work file holds an attempt: a task's attempts are its runs' task records, as the chair
    # reads them. So the cap below counts them, and a rebuild resumes from the newest one's kept patch, which another
    # lane's record carries in the store when this machine has no file of it.

    # A third run of the same task is refused outright rather than tried
    # again — quarantined here, plainly, never through `quarantine_task`,
    # because a refusal is not an attempt and must not grow the count it is
    # enforcing.
    # An `infra` attempt is the apply arm's own failure, not the task's, so it
    # must not grow the count the cap enforces. Nor does a `rescue_failed` one:
    # a rescue checks work already built and is not a build.
    read_rows = read_queue_rows(ctx)
    queue_rows = read_rows or {}
    # A task a person readied by hand, or whose initiative a person released, starts fresh: no attempt and no triage
    # request from a run at or before that point (`FRESH_AFTER_RUN`, on the task's row or the initiative's) counts. The
    # mark holds one number per run series, so a phase run (`<initiative>.<phase>-N`) is compared only within its own.
    initiative_extra = row_extra(queue_rows.get(ctx.initiative_id))
    cutoff_by_id = {
        str(item["id"]): workstore.fresh_cutoff(row_extra(queue_rows.get(str(item["id"]))), initiative_extra) for item in all_ready
    }
    # Under work_state store no work file holds an attempt: a task's attempts are its runs' task records, as the chair
    # reads them. So the cap below counts them, and a rebuild resumes from the newest one's kept patch, which another
    # lane's record carries in the store when this machine has no file of it. A record from a run at or before the
    # task's fresh start is history: it is neither an attempt nor a patch to resume from.
    recorded = {
        str(item["id"]): records_after(caused_records(ctx, phase, str(item["id"])), cutoff_by_id[str(item["id"])])
        for item in all_ready
    } if store_authoritative(ctx) else {}
    all_ready = [with_record_attempts(item, [a for a, _ in recorded.get(str(item["id"]), [])]) for item in all_ready]
    # A record the driver could not read leaves the count or the resume unsure (`unreadable_reason`): the task is held
    # for a person rather than built, so a malformed record can never relaunch it or resume it from an older patch.
    unreadable = {task_id: why for task_id, found in recorded.items() if (why := unreadable_reason(found)) is not None}
    for task_id, why in sorted(unreadable.items()):
        with ctx.items_lock:
            live = next((i for i in items if str(i["id"]) == task_id), None)
        held = live if live is not None else next(dict(i) for i in all_ready if str(i["id"]) == task_id)
        text, not_held = hold_task(ctx, held, why)
        print(f"  attempts: {task_id} held for a person: {text}" + (f" (the hold was not written: {not_held})" if not_held else ""))
    all_ready = [item for item in all_ready if str(item["id"]) not in unreadable]
    record_patches = {task_id: rescue_select.patch_of(found[-1][1]) for task_id, found in recorded.items() if found}
    attempts_by_id = {str(item["id"]): build_attempts(item.get("attempts") or []) for item in all_ready}
    # A rewritten ticket starts a fresh count. Triage still gets the whole history for its repeat guard.
    # An item with no `body` cannot be matched, so all its attempts count.
    counted_by_id = {
        str(item["id"]): [
            a
            for a in (
                workstore.attempts_on_current_body({**item, "attempts": attempts_by_id[str(item["id"])]})
                if "body" in item
                else attempts_by_id[str(item["id"])]
            )
            if workstore.counts_after(a.get("run"), cutoff_by_id[str(item["id"])])
        ]
        for item in all_ready
    }
    caused_by_id = {
        task_id: with_stored_causes({"attempts": counted}, stored_causes(ctx, task_id))["attempts"] if counted else counted
        for task_id, counted in counted_by_id.items()
    }
    cap_by_id = {task_id: attempt_cap_for(caused) for task_id, caused in caused_by_id.items()}
    # A capped task is launched into `triage` (docs/design/triage.md §1) rather
    # than quarantined outright: the graph classifies the attempt record and
    # emits by class; a triage that could not run holds the task for a person, never through `quarantine_task`.
    # A task the chair flagged for triage (its row's `TRIAGE_FLAG`) is triaged too, whatever its attempt count: a
    # failure that needs a tweak gets its instruction before any rebuild.
    triage_batch: list[dict[str, Any]] = []
    amended: dict[str, dict[str, Any]] = {}  # task id -> the item rebuilt on its append-only amended body
    # Unread rows hide the chair's flag: a task with any attempt might be one it asked triage for, so it is not built
    # in this run, before or without triage. It stays ready, and the next run reads its row.
    unread = {str(item["id"]) for item in all_ready if item.get("attempts")} if read_rows is None else set()
    for task_id in sorted(unread):
        print(f"  triage: {task_id} not built; its store row could not be read, so a request for triage cannot be ruled out")
    capped = {task_id for task_id, attempts in counted_by_id.items() if len(attempts) >= cap_by_id[task_id]} - unread
    flags = {
        task_id: flag for task_id in counted_by_id if (flag := row_extra(queue_rows.get(task_id)).get(workstore.TRIAGE_FLAG))
    }
    flagged = {t for t, flag in flags.items() if workstore.counts_after(workstore.triage_flag_run(flag), cutoff_by_id[t])}
    for task_id in sorted(set(flags) - flagged):
        drop_stale_triage_flag(ctx, task_id)
        print(
            f"  triage: {task_id} not triaged; its request came from run {workstore.triage_flag_run(flags[task_id]) or '(unknown)'}, "
            "before a person readied it or released its initiative, so it builds fresh"
        )
    capped_ids = sorted(capped | flagged)
    folded: set[str] = set()  # siblings whose body gained a combined task's scope in this phase

    def asked(task_id: str) -> str:
        """Why the task went to triage, for a line a person reads."""
        return attempt_cap_reason(attempts_by_id[task_id]) if task_id in capped else "the chair asked for triage of this quarantine."

    if capped_ids:
        all_ready_by_id = {str(item["id"]): item for item in all_ready}
        with ctx.items_lock:
            shared = {str(item["id"]): item for item in items}
            context_items = [dict(item) for item in items]
        # Under work_state store no work file holds an attempt: a flagged task's attempts are its runs' task records.
        for task_id in capped_ids:
            attempts_by_id[task_id] = attempts_by_id[task_id] or build_attempts(record_attempts(ctx, phase, task_id))
        wait_if_paused_here(ctx)
        triaged, _, triage_failures = invoke_graphs(
            [
                Invocation(
                    id=task_id,
                    graph=TRIAGE,
                    args={
                        "cartridge": ctx.cartridge,
                        "date": ctx.date,
                        "work_item": all_ready_by_id[task_id],
                        "attempts": attempts_by_id[task_id],
                        "initiative_context": triage_initiative_context(initiative, context_items, task_id),
                        "triaged": [
                            dict(r) for r in row_extra(queue_rows.get(task_id)).get(workstore.TRIAGE_RECORDS) or [] if isinstance(r, Mapping)
                        ],
                    },
                )
                for task_id in capped_ids
            ],
            specs=ctx.specs,
            runner=ctx.runner,
            run_id=f"{ctx.run_id}:{phase}",
            max_parallel=ctx.max_parallel,
        )
        def hold(task_id: str, reason: str) -> None:
            """Leave the task for a person with the mark; it is not built in this run either way."""
            text, why = hold_task(ctx, shared.get(task_id) or dict(all_ready_by_id[task_id]), reason)
            print(f"  triage: {task_id} held for a person: {text}" + (f" (the hold was not written: {why})" if why else ""))

        # A triage that could not run (its graph raised, or every answer it gave was refused) is not the task's
        # failure: quarantining again would only send it back to triage, which fails the same way. A person decides.
        for failure in triage_failures:
            task_id, reason = triage_failure_hold(failure, asked(failure.split(":", 1)[0]))
            print(f"  attempt cap: {task_id} not built; triage could not run; {newest_attempt_text(attempts_by_id[task_id])}")
            hold(task_id, reason)

        for result in triaged:
            task_id = str(result.get("run_id", "")).rsplit(":", 1)[-1]
            attempts = attempts_by_id[task_id]
            class_, diagnosis, action = str(result.get("class") or ""), str(result.get("diagnosis") or ""), str(result.get("action") or "")
            record_triage(ctx, task_id, {"class": class_, "diagnosis": diagnosis, "run": str(result.get("run_id") or "")})
            if result.get("escalated"):
                # The same (class, diagnosis) twice is the same reasoning restated: a person decides, by the mark.
                print(f"  attempt cap: {task_id} launched triage, which escalated a repeated diagnosis; {newest_attempt_text(attempts)}")
                prior = cast(Mapping[str, Any], result.get("prior") if isinstance(result.get("prior"), Mapping) else {})
                hold(task_id, f"triage repeats its earlier {class_} diagnosis (run {prior.get('run') or 'unrecorded'}): {diagnosis}")
                continue

            emitted = dict(result.get("emit") or {})
            effect = str(result.get("effect") or "")
            if emitted.get("kind") == "item_create":
                triage_batch.append(emitted)  # a platform defect's intake goes to the gate beside the hold
            if effect == "hold" or ("kind" not in emitted and effect not in ("drop", "combine")):
                print(f"  attempt cap: {task_id} launched triage -> {class_}; {newest_attempt_text(attempts)}")
                hold(task_id, f"triage {class_}: {diagnosis}" + (f" Next: {action}" if action else ""))
                continue
            if effect == "drop":
                why = drop_task(ctx, items, shared.get(task_id) or dict(all_ready_by_id[task_id]))
                if why is None:
                    print(f"  triage: {task_id} dropped: {diagnosis}")
                else:
                    hold(task_id, f"triage chose to drop this task, which did not apply ({why}): {diagnosis}")
                continue
            if effect == "combine":
                into = str(result.get("into") or "")
                # A sibling triaged in this same run may itself be dropped or folded: its answer could lose this scope.
                why = (
                    f"{into} is itself being triaged in this run" if into in capped_ids
                    else combine_task(ctx, items, shared.get(task_id) or dict(all_ready_by_id[task_id]), into)
                )
                if why is None:
                    folded.add(into)
                    print(f"  triage: {task_id} combined into {into} and dropped: {diagnosis}")
                else:
                    hold(task_id, f"triage chose to combine this task into {into}, which did not apply ({why}): {diagnosis}")
                continue

            if emitted.get("kind") == "ticket_amend":
                old_body = str(all_ready_by_id[task_id].get("body") or "")
                new_body = amended_body(old_body, str(emitted.get("suggested_action", "")))
                emitted["ramp"] = ticket_amend_ramp(old_body, new_body)
                rebuilt, failure = apply_amend_directly(
                    ctx, emitted, all_ready_by_id[task_id], old_body=old_body, new_body=new_body, phase=phase,
                    quarantined=quarantined, by_id={str(item["id"]): item for item in items},
                )
                unwritten = ""  # why the harness's own store write of the amendment did not happen; the gate then decides
                if (
                    rebuilt is None
                    and failure is None
                    and store_authoritative(ctx)
                    and apply_arm_for("ticket_amend", cast(dict[str, Any], ctx.cartridge)) in (None, "workstore")
                    and emitted["ramp"] == "eligible"
                    and is_append_only(old_body, new_body)
                ):
                    # The store is the board and no model arm is named: the harness appends the checked text itself,
                    # led by the diagnosis, which quotes the objection (check_citation refuses one that does not).
                    quoted = amended_body(old_body, f"Triage: {diagnosis}\n\n{emitted.get('suggested_action', '')}")
                    rebuilt, stored = amend_in_store(ctx, all_ready_by_id[task_id], old_body=old_body, new_body=quoted)
                    unwritten = stored or ""
                if rebuilt is not None:
                    amended[task_id] = rebuilt
                    with ctx.items_lock:
                        if task_id in shared:
                            shared[task_id]["body"] = rebuilt["body"]
                    print(
                        f"  attempt cap: {task_id} launched triage -> append-only ticket_amend applied; rebuilding; "
                        f"{newest_attempt_text(attempts)}"
                    )
                    continue
                if failure is not None and task_id not in flagged:
                    if not any(q.get("id") == task_id and q.get("grain") == "task" for q in quarantined):
                        reason = f"{attempt_cap_reason(attempts)} triage proposed an append-only amendment; applying it failed: {failure}"
                        quarantined.append({"id": task_id, "phase": phase, "grain": "task", "reason": reason, "kind": "no_work"})
                        quarantine_row(ctx, phase, task_id)
                    print(
                        f"  attempt cap: {task_id} append-only ticket_amend not applied ({failure}); "
                        f"{newest_attempt_text(attempts)}"
                    )
                    continue
                if task_id in flagged and not any(q.get("id") == task_id and q.get("grain") == "task" for q in quarantined):
                    # The chair asked for an instruction before any rebuild: without the amendment the task is not built.
                    if failure is None:
                        triage_batch.append(emitted)
                    why = failure or unwritten or "the gate decides it"
                    hold(task_id, f"triage proposed an amendment that was not applied ({why}): {action}")
                    continue
                if failure is not None:
                    continue
            triage_batch.append(emitted)
            print(
                f"  attempt cap: {task_id} launched triage -> {class_} ({emitted.get('kind')}); "
                f"{newest_attempt_text(attempts)}"
            )
    if folded:
        with ctx.items_lock:
            bodies = {str(item["id"]): item.get("body") for item in items if str(item["id"]) in folded}
        all_ready = [{**item, "body": bodies[str(item["id"])]} if str(item["id"]) in bodies else item for item in all_ready]
    all_ready = [amended.get(str(item["id"]), item) for item in all_ready]
    counted_by_id = {**counted_by_id, **{task_id: [] for task_id in amended}}
    # A task that went to triage builds only on an amended body; every other one waits under the cap as before.
    ready = [
        item for item in all_ready
        if str(item["id"]) in amended
        or (
            str(item["id"]) not in capped_ids and str(item["id"]) not in unread
            and len(counted_by_id[str(item["id"])]) < cap_by_id[str(item["id"])]
        )
    ]

    by_id = {str(item["id"]): amended.get(str(item["id"]), item) for item in items}
    if hasattr(ctx.runner, "verify_by_task"):
        ctx.runner.verify_by_task = {**ctx.runner.verify_by_task, **{t: verify_of(by_id, t) for t in by_id}}
    results: list[dict[str, Any]] = []

    # Resume: a task whose earlier run already produced an approved patch is
    # reused at no cost. Applying and checking it below is unchanged, so a
    # patch that no longer fits the phase branch quarantines on its own merits.
    reused: list[dict[str, Any]] = []
    to_run = list(ready)
    prior_results: dict[str, dict[str, Any]] = {}  # task id -> the earlier run's result for a task that is rebuilt
    if ctx.resume_from:
        to_run = []
        for task in ready:
            saved = load_result(ctx.runs_dir, ctx.resume_from, phase, str(task["id"]))
            if reusable(saved):
                reused.append(without_cause(cast(dict[str, Any], saved)))
                print(f"  reused {task['id']} from {ctx.resume_from} (approved patch, no model call)")
            else:
                to_run.append(task)
                if saved is not None:
                    prior_results[str(task["id"])] = saved
    record["reused_tasks"] = [str(r.get("ticket")) for r in reused]

    # A task whose item asks for a build budget above the cartridge's cap
    # never reaches the model at all — refused outright, plainly, the same
    # way the attempt cap above is: never through `quarantine_task`, because
    # this is not an attempt either and must not grow the count that gates it.
    cap = as_mapping(ctx.cartridge.get("policy")).get("build_budget_usd_max")
    # Item order, not set iteration order: a set's order comes from the
    # string hash seed, which is process environment, not a declared input,
    # so two runs over the same items could otherwise record and print the
    # refusals in a different order each time.
    over_budget = [
        task for task in to_run
        if task.get("budget_usd") is not None and cap is not None and task["budget_usd"] > cap
    ]
    for task in over_budget:
        task_id = str(task["id"])
        quarantined.append({
            "id": task_id, "phase": phase, "grain": "task", "kind": "no_work",
            "reason": (
                f"budget_usd {task['budget_usd']} exceeds the cartridge cap "
                f"build_budget_usd_max {cap} (a per build call ceiling)"
            ),
        })
        quarantine_row(ctx, phase, task_id)
        print(f"  build budget: {task_id} refused (budget_usd {task['budget_usd']} > cap {cap})")
    over_budget_ids = {str(task["id"]) for task in over_budget}
    within_budget = [task for task in to_run if str(task["id"]) not in over_budget_ids]

    # A surface naming a file the task creates but lacking `(new)` is healed, not
    # refused: the marker and a lint note are added, written back so a relaunch
    # does not heal again, and the build goes on with the healed task. Only a
    # malformed surface (absolute, outside the repository, a glob) is refused
    # plainly, like the budget refusal above: not an attempt, so never through
    # `quarantine_task`, and no model call is made.
    # Judged against the fetched default branch, not the checkout, which may be stale.
    surface_paths = frozenset[str]()
    if any(task.get("surfaces") for task in within_budget):
        listed = default_branch_paths(ctx)
        surface_paths = listed if listed is not None else working_tree_paths(ctx.repo)
    refused_ids: set[str] = set()
    healed_by_id: dict[str, dict[str, Any]] = {}
    heal_notes: dict[str, list[str]] = {}
    ticket_corrections: dict[str, list[str]] = {}
    # The ticket check reads file contents at the commit the listing was taken from. It never refuses or
    # quarantines: when the commit cannot be read, the check raises, is logged, and the task builds unchanged.
    commit = default_branch_commit(ctx) if surface_paths else ""
    defined, search = ticket_lookups(str(ctx.repo), commit, git_stdout)
    for task in (t for t in within_budget if t.get("surfaces")):
        task_id = str(task["id"])
        bad = malformed_surfaces([str(s) for s in task["surfaces"]])
        if bad:
            refused_ids.add(task_id)
            quarantined.append({
                "id": task_id, "phase": phase, "grain": "task", "kind": "no_work", "reason": surface_refusal_reason(bad),
            })
            record_task_cause(ctx, phase, task_id, "ticket", "rule: surfaces not in the repository")
            quarantine_row(ctx, phase, task_id)
            print(f"  surfaces: {task_id} refused (malformed: {', '.join(bad)})")
            continue
        # The ticket check runs first: a bare filename must be resolved before heal_new_markers can mark it `(new)`.
        # Prose notes are moved out of surfaces first, so the check does not read them as filenames.
        prepared = notes_out_of_surfaces(task)
        try:
            checked, corrections = check_ticket(prepared, surface_paths, defined, search)
        except Exception as exc:
            epic_log.warning("ticket check failed, task builds unchanged: task=%s: %s: %s", task_id, type(exc).__name__, exc)
            checked, corrections = prepared, ()
        if corrections:
            ticket_corrections[task_id] = list(corrections)
        healed = without_correction_lint(heal_surfaces(checked, surface_paths), task)
        if healed == dict(task):
            if corrections:
                print(f"  ticket check: {task_id} {len(corrections)} correction(s) for the builder")
            continue
        healed_by_id[task_id] = healed
        known = {str(n) for n in task.get("lint") or []}
        heal_notes[task_id] = [str(n) for n in healed.get("lint") or [] if str(n) not in known]
        write_healed_surfaces(
            ctx, task_id, healed["surfaces"], heal_notes[task_id], str(task["path"]) if task.get("path") else None
        )
        changed = [str(s) for s in healed["surfaces"] if s not in task["surfaces"]]
        label = "ticket check" if checked != prepared else "surfaces"
        extra = f", {len(corrections)} correction(s)" if corrections else ""
        print(f"  {label}: {task_id} healed ({', '.join(changed) or 'surfaces unchanged'}){extra}")
    runnable = [healed_by_id.get(str(t["id"]), t) for t in within_budget if str(t["id"]) not in refused_ids]

    # A task the driver quarantined before carries its reasons — and, when one
    # was saved, its last patch — into the ticket body, so the planner and the
    # builder both see what already failed and why, next to the critique.
    # Offered as a reference; nothing here approves it.
    patches_for_attempt: dict[str, str | None] = {
        str(task["id"]): ((load_result(ctx.runs_dir, task["attempts"][-1]["run"], phase, str(task["id"])) or {})
                           .get("build") or {}).get("patch") or record_patches.get(str(task["id"]))
        for task in runnable
        if task.get("attempts")
    }

    # The phase worktree is shared by the phase's tasks, so the base is checked once, before any build. A base
    # missing merged work is rebuilt by merging that work in; one that cannot be rebuilt stops the phase. No
    # task is quarantined and no attempt is recorded: the tasks stay ready for the next launch.
    if runnable:
        worktree = ctx.phase_worktree(phase)
        expected = base_expected(ctx, phase, parents, items, runnable)
        missing = base_gaps(worktree, expected)
        if missing:
            fetch_origin(ctx.repo)
            merged_ok, merge_detail = merge_in(worktree, [g.commit for g in missing])
            remaining = base_gaps(worktree, expected)
            if remaining or not merged_ok:
                stopped = remaining or missing
                record["status"] = "blocked"
                record["reason"] = stale_base_stop(stopped)
                quarantined.append({
                    "id": stopped[0].task, "phase": phase, "grain": "phase", "kind": "stale_base",
                    "reason": record["reason"],
                })
                git_in("-C", str(worktree), "checkout", "--detach", "-q")
                print(f"  base check: {phase} stopped ({record['reason']}; {merge_detail or 'commit not found'})")
                return record
            record["base_rebuilt"] = sorted({g.task for g in missing})
            print(f"  base check: {phase} base rebuilt with {', '.join(record['base_rebuilt'])}")

    # A rebuild works from what the task already built. The last attempt is put back, in full, in a tree of its own;
    # the builder reads that tree and returns only the change; the two are composed below into the one patch that is
    # applied, checked and merged. Nothing finished is regenerated from a truncated reference.
    resumed: dict[str, tuple[Path, str]] = {}
    for task in runnable:
        last = patches_for_attempt.get(str(task["id"]))
        if last and last.strip() and (tree := attempt_tree(ctx, phase, str(task["id"]), last)) is not None:
            resumed[str(task["id"])] = tree

    budget_sources: dict[str, Any] = {}
    if runnable:
        wait_if_paused_here(ctx)
        invocations = [
            lifecycle_invocation(
                ctx,
                task,
                body=plan_context(
                    {
                        **task,
                        "body": (
                            resume_body(task.get("body") or "", list(task.get("attempts") or []), resumed[str(task["id"])][1])
                            if str(task["id"]) in resumed
                            else carry_forward(
                                task.get("body") or "",
                                list(task.get("attempts") or []),
                                patches_for_attempt.get(str(task["id"])),
                                limit=PATCH_FOR_VALIDATION_CHARS,
                            )
                        )
                        + "".join(f"\n\nSurface note: {note}" for note in heal_notes.get(str(task["id"]), []))
                        + "".join(f"\n\nCorrection note: {note}" for note in ticket_corrections.get(str(task["id"]), []))
                        + "".join(
                            f"\n\n{block}"
                            for block in (previous_objection_block(prior_results.get(str(task["id"]))),)
                            if block
                        ),
                    },
                    handoffs,
                ),
                fix_attempts=ctx.fix_attempts,
            )
            for task in runnable
        ]
        budget_sources = {inv.id: inv.args["build_budget_source"] for inv in invocations}
        results, _, failures = invoke_graphs(
            [inv for inv in invocations if inv.id not in resumed],
            specs=ctx.specs,
            runner=ctx.runner,
            run_id=f"{ctx.run_id}:{phase}",
            max_parallel=ctx.max_parallel,
        )
        for inv in (i for i in invocations if i.id in resumed):
            more, more_failures = invoke_from_attempt(ctx, phase, inv, *resumed[inv.id], run_id=f"{ctx.run_id}:{phase}")
            results = [*results, *more]
            failures = [*failures, *more_failures]
        # A child's failure is a quarantined task, not a failed swarm — the
        # policy `invoke_graphs` names continue-and-quarantine. A lapsed login is
        # the host's failure, not the task's: like the account session limit it
        # records no attempt and leaves the task ready, so the next launch on a
        # logged-in host builds it (2026-10-23: two tasks sat quarantined after an
        # expired login, and `runs cause` could not reach an attempt to retry).
        for failure in failures:
            if is_auth_failure(failure):
                continue
            failed = failure.split(":", 1)[0]
            # An empty patch from a base that lacks the ticket's own files is the base's fault, not the ticket's.
            empty = is_patch_empty(failure) and failed in by_id
            missing = stale_base_missing(ctx, phase, by_id[failed]) if empty else ()
            # Files that all live in one other repository are the ticket's fault: its cause is `ticket`, no free retry.
            elsewhere = wrong_repository(missing, repo_key(str(ctx.repo)))
            stale_reason = (
                wrong_repository_reason(failed, missing, elsewhere) if elsewhere else stale_base_reason(failed, missing)
            )
            quarantined.append(
                quarantine_task(
                    ctx, by_id, phase=phase, task=failed,
                    reason=stale_reason if missing else failure,
                    kind="stale_base" if missing else invoke_failure_kind(failure),
                )
            )
        # A lapsed login fails every task in every later phase the same way.
        first_auth = next((f for f in failures if is_auth_failure(f)), None)
        if first_auth is not None:
            record["stopped"] = f"the runner cannot authenticate: {first_auth[:200]}"
    # Every result — fresh or reused — is saved under THIS run, so the next
    # resume has one place to look and the record of what ran is complete.
    results = [*reused, *results]
    for result in results:
        # `cox runs land`/`recover` resolve a phase branch as `epic/<initiative>/<phase>`;
        # without these two fields on the saved record, neither command can find it.
        result.setdefault("initiative", ctx.initiative_id)
        result.setdefault("phase", phase)
        result.update(body_stamp(by_id.get(str(result.get("ticket")))))
        save_task_result(ctx, result, phase=phase, task=str(result.get("ticket")))

    built: dict[str, dict[str, Any]] = {}
    surviving: list[str] = []
    escalated: set[str] = set()

    for result in sorted(results, key=lambda r: str(r.get("ticket"))):
        task = str(result.get("ticket"))

        # A non-build node raised mid-round: review, adversary, arbitrate,
        # validate or handoff never reached a verdict, so nothing here was
        # judged and `unapproved` has nothing to read. The patch a later
        # node was handed is kept rather than lost to the traceback.
        failed_node = result.get("failed_node")
        if failed_node is not None:
            patch = str((result.get("build") or {}).get("patch") or "")
            detail = str(result.get("failed_node_reason") or f"node '{failed_node}' failed")
            # Prefixed so `is_harness_fault` recognises it: this task's
            # quarantine never went through `built[task]`, so the shared
            # outcome loop below reads no verdicts for it and would otherwise
            # call `task_outcome` "rejected" — the same label a patch two
            # reviewers actually declined gets, for a patch nothing judged.
            reason = f"{HARNESS_FAULT_PREFIX} {detail}"
            record["task_records"].append(
                {
                    "id": task,
                    "phase": phase,
                    "branch": ctx.scratch_branch(task),
                    "evidence": [{"check": "fix_loop", "output": reason}],
                    "quarantine": reason,
                    "governance_hits": [],
                    "draft": None,
                    "merged": False,
                    "status": "quarantined",
                    "build": {"patch": patch},
                    "failed_node": failed_node,
                }
            )
            quarantined.append(quarantine_task(ctx, by_id, phase=phase, task=task, reason=reason, kind="infra"))
            continue

        # The loop's refusal is the answer, and it is free. Applying a patch the
        # reviewers rejected costs a worktree, a check run and both validators
        # before anything says no, and what finally says no is a validator
        # reading a stale verdict rather than the loop that actually decided.
        refused = unapproved(result)
        if refused is not None:
            record["task_records"].append(
                {
                    "id": task,
                    "phase": phase,
                    "branch": ctx.scratch_branch(task),
                    "evidence": [{"check": "fix_loop", "output": refused}],
                    "quarantine": refused,
                    "governance_hits": [],
                    "draft": None,
                    "merged": False,
                    "status": "quarantined",
                }
            )
            quarantined.append(
                quarantine_task(ctx, by_id, phase=phase, task=task, reason=refused, kind="refused", result=result)
            )
            continue

        build = build_task(ctx, phase=phase, task=task, result=result, verify=verify_of(by_id, task))
        final, build = refix_task(ctx, by_id, phase=phase, task=task, result=result, build=build)
        build["result"] = final
        built[task] = build
        if "checks" in build:
            # The result file was saved before the checks ran. `runs detail` reads that file, so the
            # per-check results (excerpt and log name) go into it under `change_facts`, as the
            # task record below carries them.
            facts = {**(final.get("change_facts") or {}), "checks": build["checks"]}
            save_task_result(ctx, {**final, "change_facts": facts}, phase=phase, task=task)

        # Evidence first, escalation second — the same order `cli.py` uses, and
        # for the same reason: the gate should see the tests' opinion of a
        # governance change too.
        for item in final.get("proposals") or []:
            if item.get("kind") in DRAFT_KINDS:
                item.setdefault("evidence", []).extend(build["evidence"])

        # A change to the rules is not whatever kind the graph called it. This
        # runs after emission and before the policy split, which is the only
        # window where no streak on a mundane kind can carry a governance edit
        # past the gate.
        build["proposals"], hits = escalate_self_modification(
            final.get("proposals") or [],
            patch=str((final.get("build") or {}).get("patch") or ""),
            cartridge=ctx.cartridge,
            ledger_path=ctx.ledger_path,
        )
        if hits:
            escalated.add(task)
            build["governance_hits"] = hits

        # `execute_cleared` sees only the batch item, so the verdicts ride on it.
        arbitration = final.get("arbitration")
        for item in build["proposals"]:
            if item.get("escalated_from") == "draft_pr_create":
                item["review_verdict"] = str((final.get("review") or {}).get("verdict") or "")
                item["arbitration_verdict"] = str(
                    arbitration.get("verdict") if isinstance(arbitration, Mapping) else arbitration or ""
                )

        test_database: _TaskExtras = {"test_database": build["test_database"]} if "test_database" in build else {}
        refix: _TaskExtras = {"refix": build["refix"]} if build.get("refix") else {}
        record["task_records"].append(
            {
                "id": task,
                "phase": phase,
                "branch": build["branch"],
                "evidence": build["evidence"],
                "quarantine": build.get("quarantine"),
                # A quarantined task never reaches `surviving` below, so this is
                # the only task record it gets — the per-check results have to
                # land here too, not only on the `phase_state` a survivor's
                # validator sees.
                "change_facts": {
                    **(build["result"].get("change_facts") or {}),
                    **({"checks": build["checks"]} if "checks" in build else {}),
                },
                "governance_hits": hits,
                "draft": None,
                "merged": False,
                "status": "quarantined" if build.get("quarantine") else "built",
                **test_database,
                "lint_fixed": build.get("lint_fixed", False),
                "lint_fix_checks": build.get("lint_fix_checks", []),
                "build_budget_source": budget_sources.get(task),
                **refix,
            }
        )
        if build.get("quarantine"):
            reason = build["quarantine"]
            # A harness fault is not an attempt: the build was never actually
            # tried, so recording one here would burn the same two-strike cap
            # a real failure does. Mirrors the attempt-cap refusal above,
            # which quarantines without ever calling `quarantine_task`.
            if is_harness_fault(reason):
                quarantined.append({"id": task, "phase": phase, "grain": "task", "reason": reason, "kind": "no_work"})
                quarantine_row(ctx, phase, task)
            else:
                # This branch is only reached for a task the fix loop already
                # approved — `unapproved` above quarantines anything else as
                # `refused` before a worktree is even opened. A check still
                # failing here, after the lint_fix step had its chance, is a
                # non-functional finding on an approved patch, not grounds to
                # discard it: `unverified` keeps it, same as `validate_chunk`.
                detail = check_feedback(build.get("checks") or []) or None
                quarantined.append(
                    quarantine_task(
                        ctx, by_id, phase=phase, task=task, reason=reason, kind="unverified", detail=detail
                    )
                )
            continue
        surviving.append(task)

    # ── validation, between the fan-out and the merges ───────────────────────
    chunk_by_task: dict[str, dict[str, object]] = {}
    verdict: dict[str, Any] | None = None
    validated = "validate_phase" in ctx.bound

    # A phase is judged once, when it is finished: a run carrying only part of it
    # would be scored against a goal its siblings have not yet built toward.
    # A task an earlier run approved counts as finished, and joins the validator's input.
    # One store read: a task another machine landed since this run read its items counts as finished.
    store_rows = read_store_rows(ctx)
    with ctx.items_lock:
        fresh_items = store_fresh(items, store_rows)
    earlier = earlier_merged(fresh_items, phase, {str(r.get("ticket")) for r in results})
    open_ids = (
        open_in_phase(
            fresh_items,
            phase,
            {str(r.get("ticket")) for r in results} | {str(q["id"]) for q in quarantined} | {str(i["id"]) for i in earlier},
            ctx.land_at,
        )
        if validated and results
        else []
    )

    if open_ids:
        verdict = record["phase_verdict"] = {
            "goal_met": False,
            "skipped": f"phase not finished: {len(open_ids)} open task(s): {', '.join(open_ids)}",
        }
    elif validated and results:
        phase_state = {
            "phase": {"id": phase, "goal": phase_goal(initiative, phase)},
            "tasks": [
                {
                    "id": task,
                    "title": str((by_id.get(task) or {}).get("title") or task),
                    # The work store's own prose, never the builder's summary of
                    # what it did. See `graphs/delivery/phase_validate.py`: a
                    # validator handed the owner's account is reviewing a
                    # recollection, and the graph strips one if it arrives.
                    "description": str((by_id.get(task) or {}).get("body") or ""),
                    "evidence": built[task]["evidence"],
                    "change_facts": {
                        **(built[task]["result"].get("change_facts") or {}),
                        **({"checks": built[task]["checks"]} if "checks" in built[task] else {}),
                    },
                    "review_verdict": str((built[task]["result"].get("review") or {}).get("verdict") or ""),
                    # The patch is machine evidence — the diff git applied — not
                    # the builder's account of it. A validator without it said,
                    # in its own words, that it could not verify anything.
                    "patch": str((built[task]["result"].get("build") or {}).get("patch") or "")[:PATCH_FOR_VALIDATION_CHARS],
                    # Where `validate_chunk`'s needs_evidence, if any, gets
                    # read from — still on disk here, since validation runs
                    # before the merges that would retire it.
                    "worktree": str(ctx.task_worktree(phase, task)),
                }
                for task in surviving
            ]
            + [
                {
                    "id": str(item["id"]),
                    "title": str(item.get("title") or item["id"]),
                    "description": str(item.get("body") or ""),
                    "evidence": [],
                    "change_facts": {
                        k: list((item.get("handoff") or {}).get(k) or []) for k in ("files_changed", "interfaces_added", "decisions")
                    },
                    "review_verdict": "approve",
                    "patch": "",
                    "worktree": str(ctx.task_worktree(phase, str(item["id"]))),
                }
                for item in earlier
            ],
            "quarantined": [{"id": q["id"], "reason": q["reason"]} for q in quarantined],
        }
        validations, _, failures = invoke_graphs(
            [
                Invocation(
                    id=f"{VALIDATE}:{phase}",
                    graph=VALIDATE,
                    args={
                        "date": ctx.date,
                        "cartridge": ctx.cartridge,
                        "phase_state": phase_state,
                        "reader": read_evidence_file,
                    },
                )
            ],
            specs=ctx.specs,
            runner=ctx.runner,
            run_id=ctx.run_id,
            max_parallel=1,
        )
        if validations:
            record["chunk_verdicts"] = list(validations[0].get("chunk_verdicts") or [])
            verdict = record["phase_verdict"] = dict(validations[0].get("phase_verdict") or {})
            chunk_by_task = {str(v.get("task")): v for v in record["chunk_verdicts"]}
        else:
            # `validate_phase` raised for the WHOLE batch, not a single task —
            # `chunk_by_task` stays empty, and the loop below only quarantines
            # a task whose chunk verdict is present AND unsatisfied, so every
            # surviving task reaches the gate with no verdict at all rather
            # than a per-task `infra` quarantine. That is a real gap of the
            # same shape this ticket closes for a single task's node calls,
            # but closing it here means deciding what a WHOLE PHASE does when
            # its one validation call fails — hold every task, or requarantine
            # the batch — which this ticket's per-task scope does not cover.
            record["reason"] = f"the validator failed: {'; '.join(failures)}"

    # An unsatisfied chunk verdict quarantines its task BEFORE the gate. A task
    # the validator says did not do what it said is not a task whose merge
    # should be up for a decision.
    for task in list(surviving):
        chunk = chunk_by_task.get(task)
        if chunk is not None and not chunk.get("satisfied"):
            surviving.remove(task)
            gaps = ", ".join(str(g) for g in as_list(chunk.get("gaps"))) or str(chunk.get("reasoning", ""))
            quarantined.append(
                quarantine_task(
                    ctx, by_id, phase=phase, task=task,
                    reason=f"validate_chunk unsatisfied: {gaps}", kind="unverified",
                )
            )
            for task_record in record["task_records"]:
                if task_record["id"] == task:
                    task_record["status"] = "quarantined"
                    task_record["quarantine"] = f"validate_chunk unsatisfied: {gaps}"

    # ── the phase's one gate batch, in task-id order ─────────────────────────
    batch, slots = build_batch(
        ctx,
        phase=phase,
        surviving=surviving,
        built=built,
        escalated=escalated,
        chunk_by_task=chunk_by_task,
        rebase=None,
        by_id=by_id,
    )
    batch = batch + triage_batch
    record["batch"] = batch

    # ── policy, then the gate, then execution in batch order ────────────────
    auto, gated = policy_split(ctx, batch)
    # At ticket, with no `assume` given, nobody is asked and nobody decides: the batch is auto-cleared. Auto-cleared
    # work writes no gate diff and no ledger row, so unattended runs add nothing to a kind's graduation streak.
    if land_gate_level(ctx.land_at) == "initiative" and ctx.assume is None:
        auto, gated = [*auto, *gated], []
    auto_ids = {id(item) for item in auto}
    decisions, human_minutes = gate(gated, assume=ctx.assume) if gated else ([], 0.0)
    decided = {id(item): (decision, edited) for item, decision, edited in decisions}

    state = Execution(
        landed={}, merged={}, moved={}, quarantined=quarantined, escalated=frozenset(escalated),
        builds={task: (built[task]["result"].get("build") or {}) for task in surviving},
    )  # fmt: skip
    diffs: list[dict[str, Any]] = []

    for item in batch:
        slot, subject = slots.get(id(item), ("other", ""))
        if id(item) in auto_ids:
            applied, _ = execute_cleared(ctx, item, slot=slot, subject=subject, phase=phase, state=state, by_id=by_id)
            # Auto-cleared: NO gate diff and NO ledger row. Autonomy is spent by
            # acting; a row here would let a kind ratchet itself up on its own
            # say-so, which is the self-report the ledger exists to disbelieve.
            record["rebased"] = record["rebased"] or (applied and slot == "rebase")
            continue

        decision, edited = decided.get(id(item), ("refused", False))
        applied = False
        if decision == "approved":
            applied, _ = execute_cleared(ctx, item, slot=slot, subject=subject, phase=phase, state=state, by_id=by_id)
            record["rebased"] = record["rebased"] or (applied and slot == "rebase")
        # Built here rather than by `gate.apply_decisions`, which cannot know
        # about a branch this driver created: `applied` is what actually
        # happened, so an approved merge that conflicted records `skipped`.
        diffs.append(gate_diff(item, decision, applied=applied, edited=cast(bool, edited)))

    for warning in state.push_warnings:
        note_push_failure(record, warning)

    # At `task` each task merged by this run gets its own PR; at `level` and `ticket` none does. A failed PR is a
    # note on the task, not a quarantine: the merge into the phase branch already happened.
    if opens_pr(ctx.land_at, "task"):
        held = {q["id"] for q in state.quarantined if q.get("grain") == "task"}
        # A restart also covers the tasks a first run merged and approved, whose PR may never have opened.
        stored_merged = {str(i["id"]) for i in items if i.get("phase") == phase and i.get("state") == "approved" and i.get("merged")}
        for task in sorted(({t for t in surviving if state.merged.get(t)} | stored_merged) - held):
            ok, detail = open_grain_pr(
                ctx,
                grain="task",
                name=task,
                local=ctx.draft_branch(phase, task),
                title=f"{ctx.initiative_id}: {by_id[task]['title']}",
                base=ctx.default_ref.removeprefix("origin/"),
            )
            if ok:
                state.review_pr[task] = detail
            else:
                state.notes[task] = detail

    # `execute_cleared` can quarantine a task after its merge already succeeded — the
    # apply arm failing on its own bookkeeping, not the code — so that failure
    # must reach the task record here, or reconciliation below would see only
    # `merged=True` and report it `landed`.
    quarantined_by_task = {q["id"]: q["reason"] for q in state.quarantined if q.get("grain") == "task"}

    for task_record in record["task_records"]:
        task = task_record["id"]
        task_record["draft"] = ctx.draft_branch(phase, task) if state.landed.get(task) else None
        task_record["merged"] = bool(state.merged.get(task))
        if task in quarantined_by_task and not task_record.get("quarantine"):
            task_record["status"] = "quarantined"
            task_record["quarantine"] = quarantined_by_task[task]
        elif state.merged.get(task) is False and not task_record.get("quarantine"):
            task_record["status"] = "quarantined"
        elif not task_record["merged"] and not task_record.get("quarantine"):
            # Approved but its merge was never attempted — escalated to
            # `self_modification`, or any other reason `merge_stack` did not
            # run — so the task is not `done` yet, only `approved`.
            task_record["status"] = "approved"
        if task in state.review_pr:
            task_record["review_pr"] = state.review_pr[task]
        verdicts = (built.get(task) or {}).get("result") or {}
        task_record["outcome"] = task_outcome(
            str((verdicts.get("review") or {}).get("verdict") or "") or None,
            # A skipped arbiter is recorded as a string, not a mapping.
            str(cast(Mapping[str, Any], verdicts.get("arbitration") if isinstance(verdicts.get("arbitration"), Mapping) else {}).get("verdict") or "") or None,
            task_record.get("quarantine"),
            # Merged AND quarantined only happens when `execute_cleared`'s own
            # bookkeeping step failed after the code already landed — that is
            # not `landed` in the sense this outcome reports.
            task_record["merged"] and not task_record.get("quarantine"),
        )
        task_record["reason"] = task_record.get("quarantine") or state.notes.get(task)

    # An executed `state_move` is reflected in the driver's own copy of the work
    # so the next phase's tasks can become ready inside this run. `run_epic`
    # lands nothing itself — only `cox runs land` writes `done` (tools #153) —
    # so every moved task reads `approved` here, whether it merged into the
    # phase stack or not. `item["merged"]` is a second, internal-only field
    # (never returned to a caller) that `_ready_view` below reads to tell a
    # merged-but-unlanded parent from an escalated one that never reached the
    # stack at all.
    for task, moved in state.moved.items():
        if moved:
            merged = bool(state.merged.get(task))
            with ctx.items_lock:
                for item in items:
                    if str(item["id"]) == task:
                        item["state"] = "approved"
                        item["merged"] = merged
                        if merged and task in state.commits:
                            item["merged_commit"] = state.commits[task]
                        if task in state.handoffs:
                            item["handoff"] = state.handoffs[task]
            for task_record in record["task_records"]:
                if task_record["id"] == task:
                    task_record["state"] = "approved"

    record_tasks(ctx, phase, record["task_records"], quarantined)

    record["status"], reason = phase_status(
        verdict,
        validated=validated,
        ready=ready,
        quarantined=quarantined,
        items=store_fresh(items, store_rows),
        phase=phase,
        landed=bool(surviving) and all(state.merged.get(task) for task in surviving),
        branch=branch,
        land_at=ctx.land_at,
    )
    if reason:
        record.setdefault("reason", reason)

    # The level gate: once per assembled level, after the status is known and before any review PR opens.
    # It covers the level as merged. The optional style_pass trim below runs later and is not re-checked here.
    if record["status"] == "complete":
        run_level_checks(ctx, phase, record)
    if record["status"] == "complete":
        run_level_integration(ctx, phase, str(record["base"]), record)

    # docs/design/landing-model.md §5: one style_pass over the assembled phase
    # diff, run only once validate_phase itself has said the goal is met — a
    # trim is not a second opinion on completeness, it runs after that
    # question is already settled.
    if record["status"] == "complete" and verdict is not None and verdict.get("goal_met") and "style_pass" in ctx.bound:
        stale_trim = fence_reason(ctx)
        outcome = f"refused: {stale_trim}" if stale_trim else trim_phase(ctx, phase)
        if outcome is not None:
            record["trim"] = f"trim: {outcome}"

    # Release the phase branch. Git refuses to check one branch out in two
    # worktrees, and re-entrancy — a later run building on the branch this one
    # left — is a stated requirement, so the worktree keeps its files and gives
    # the branch back. Nothing here deletes work: the branches are the artifact.
    git_in("-C", str(ctx.phase_worktree(phase)), "checkout", "--detach", "-q")

    totals = {
        "ready": len(ready),
        "completed": len(results),
        "surviving": len(surviving),
        "quarantined": sum(1 for q in quarantined if q.get("grain") == "task"),
        "auto_applied": len(auto),
        "gated": len(gated),
    }
    manifest = build_manifest(
        run_id=f"{ctx.run_id}:{phase}",
        ts=datetime.now(UTC).isoformat(),
        principal=PRINCIPAL,
        cartridge=ctx.cartridge,
        provider_profile=ctx.provider_profile,
        proposals=batch,
        gate_diffs=diffs,
        human_minutes=human_minutes,
        totals=totals,
    )
    record["totals"] = totals
    stale = fence_reason(ctx)
    if stale is not None:
        return stale_phase(record, stale)
    append_ledger(manifest, ledger_path=ctx.ledger_path)  # the host file is written once, never inside a retry
    if ctx.store is not None:
        store = ctx.store
        # Ledger rows are built in `core.manifest.append_ledger`; the store copies this phase's rows back by run id.
        for row in ledger.read(ctx.ledger_path):
            if row.get("run_id") == manifest["run_id"]:
                ctx.retry.write(lambda row=row: store.record_ledger(row, epoch=ctx.epoch), END_WRITE_WINDOW_S)
        ctx.retry.write(
            lambda: store.record_gate_decisions(ctx.run_id, phase, diffs, epoch=ctx.epoch), END_WRITE_WINDOW_S
        )
    record["manifest"] = f"{ctx.run_id}:{phase}"
    record["manifest_record"] = manifest
    return record
