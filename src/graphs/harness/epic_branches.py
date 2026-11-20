"""The epic driver's branches: the initiative branch, review branches and PRs, the default branch and carry-forward."""

from __future__ import annotations

import os
import time
import types
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from tempfile import gettempdir
from typing import cast

from core import landing_record, workstore
from core.jsontypes import is_mapping
from core.landing import TemplateError, initiative_branch_name, pr_branch_name, resolve_push_initiative
from core.landing_record import INTEGRATE_PHASE
from harness import bounded_proc, courier_adapter, landing_git, landing_ops, landing_store
from harness.check_gate import gate_commands, repo_gate_lock
from harness.checks import outside_surface_files, run_checks
from harness.digest import build_digest
from harness.epic_context import Ctx, git_in
from harness.epic_phase_branch import branch_exists, publish_branch
from harness.epic_records import END_WRITE_WINDOW_S, ctx_store, utc_now
from harness.epic_types import EpicItem, PhaseRecord, as_list, as_mapping
from harness.invoke import Invocation, invoke_graphs
from harness.lane_pytest_tmp import lane_scratch_dir, lane_tmp_env, run_scratch_root
from harness.lane_workers import workers_env
from harness.phase_remote import (
    FetchOutcome,
    Kind,
    fetch_branch,
    materialise_local,
    missing_branch_cause,
    settle_diverged,
)
from harness.worktree import apply_patch, registry_lock, remove_worktree

LIFECYCLE = "lifecycle"
VALIDATE = "validate"
# `graphs/ops/triage_quarantine.py`'s `GRAPH_NAME` is "triage" (docs/design/triage.md
# §1), but its `SPEC.name` — the registry key `invoke_graphs` looks up — is
# "triage-quarantine", because "triage" already names alert triage's subcommand.
TRIAGE = "triage-quarantine"


# Commits the driver makes are mechanical — it saves an applied patch and joins
# branches, it never authors. The identity is passed with -c so a test, or a
# machine with no global git config, needs no setup to run this.
IDENTITY = ("-c", "user.email=epic-swarm@invalid", "-c", "user.name=epic-swarm")

# Seconds a `gh pr` call may run: the one value bounded_proc and the tools bounded_run share.
_GH_TIMEOUT_SECONDS = bounded_proc.GH_TIMEOUT_SECONDS

def opens_pr(land_at: str, grain: str) -> bool:
    """A review PR is opened per `task` only at land_at `task`. No phase ever opens one: the retired `level` reads as
    `initiative`, where the one pull request is the initiative's and the lander opens it."""
    return grain == "task" and land_at == "task"


DRAFT_KINDS = frozenset({"draft_pr_create", "self_modification"})

# How much of a task's patch the validators see. All of it for any real task;
# the bound only stops a pathological diff from swamping the phase prompt.
PATCH_FOR_VALIDATION_CHARS = 120_000

# Two recorded attempts and a third run is refused rather than tried again.
# A refusal past this point is not itself an attempt, so it must not grow the
# count it is enforcing — see `run_phase_tasks`, which quarantines these tasks with
# a plain `quarantined.append` and `quarantine_row` rather than `quarantine_task`.
ATTEMPT_CAP = 2


# docs/design/landing-model.md §5: the existing style_pass brief, plus the one
# sentence that scopes it to a whole phase rather than one task's diff.
STYLE_PASS_PROMPT = (
    "Make the narrow style edit an approved diff still needs, or return nothing. "
    "Remove duplication the tickets introduced separately; remove shims whose exit "
    "condition the phase has met."
)

CHECK_FIX_PROMPT = (
    "The diff below was approved, then a configured check failed on it. Make the smallest "
    "edit that fixes the failure shown, as a unified diff against the tree with the "
    "approved diff already applied, or return nothing."
)

STYLE_PASS_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"patch": {"type": "string"}},
    "required": ["patch"],
    "additionalProperties": False,
}


def carry_forward(body: str, attempts: Sequence[Mapping[str, object]], patch: str | None, *, limit: int) -> str:
    """Append a task's own quarantine history to its ticket body, as a reference.

    Pure: no file, no clock. With no attempts the body is untouched — most
    tasks have none. Otherwise the planner and the builder both see why the
    last try was quarantined, and — when a patch was actually saved — what it
    looked like, offered as a reference and never as an approved change.
    """
    if not attempts:
        return body
    header = (
        "",
        "## Previous attempts (recorded by the harness)",
        *(f"- {a.get('run')} ({a.get('phase')}): {a.get('reason')}" for a in attempts),
    )
    reference = (
        (
            "",
            "Reference patch from the last attempt (NOT approved — the reasons above are "
            "why; apply what still fits, address every reason, and re-read the critique "
            "before trusting any line of it):",
            "```diff",
            patch[:limit] if len(patch) <= limit else f"{patch[:limit]}\n... truncated",
            "```",
        )
        if patch and patch.strip()
        else ()
    )
    return "\n".join((body, *header, *reference))


RESUME_NOTE = (
    "Your previous attempt is in the tree you are working in, in full, as commit {sha} on top of the phase branch. "
    "It was not approved, for the reasons above. Work from it: change only what those reasons need, and return a "
    "patch against this tree as it stands. Do not rebuild, rewrite or remove what is already there."
)


def resume_body(body: str, attempts: Sequence[Mapping[str, object]], sha: str) -> str:
    """Pure. The ticket body for a rebuild that starts from its last attempt: the reasons, and where that work is."""
    return "\n".join((carry_forward(body, attempts, None, limit=0), "", RESUME_NOTE.format(sha=sha[:12])))


def attempt_tree(ctx: Ctx, phase: str, task: str, patch: str) -> tuple[Path, str] | None:
    """Edge. A detached worktree at the phase tip holding the task's last attempt, applied in full and committed.

    (the tree, the commit). None when the attempt no longer applies to the phase as it stands now: the rebuild then
    gets the attempt as a reference patch, as before. The tree lives under the run directory and goes with it."""
    tree = ctx.worktree_root / ctx.run_id / f"{phase}--attempt--{task}"
    tree.parent.mkdir(parents=True, exist_ok=True)
    with registry_lock(ctx.repo):
        ok, _ = git_in("-C", str(ctx.repo), "worktree", "add", "--detach", str(tree), ctx.phase_branch(phase))
    if not ok:
        return None
    applied, _ = apply_patch(patch, tree)
    staged = applied and git_in("-C", str(tree), "add", "-A")[0]
    committed = staged and git_in(
        *IDENTITY, "-C", str(tree), "commit", "-q", "--no-verify", "-m", f"epic {ctx.run_id}: previous attempt of {task} (not approved)"
    )[0]
    ok, sha = git_in("-C", str(tree), "rev-parse", "HEAD") if committed else (False, "")
    if not ok:
        remove_worktree(ctx.repo, tree)
        return None
    return tree, sha


def _compose_with_attempt(ctx: Ctx, phase: str, tree: Path, patch: str) -> str | None:
    """Edge. The last attempt and the rebuild's change as one patch against the phase tip, which is what gets applied,
    checked and merged. None when the change does not apply to the attempt tree it was written against."""
    if not apply_patch(patch, tree)[0] or not git_in("-C", str(tree), "add", "-A")[0]:
        return None
    ok, combined = git_in("-C", str(tree), "diff", "--cached", "--binary", ctx.phase_branch(phase))
    return combined + "\n" if ok and combined.strip() else None


def invoke_from_attempt(
    ctx: Ctx, phase: str, inv: Invocation, tree: Path, attempt_sha: str, *, run_id: str
) -> tuple[list[dict[str, object]], list[str]]:
    """Edge. Run one rebuild with the runner reading the attempt's tree, then make its result's patch the whole change.

    The record keeps both: `patch` is what is applied to the phase tip, `change` is what the rebuild did to the last
    attempt. A change that does not apply to the tree it was written against is left as the builder returned it."""
    view = PhaseRunner(
        ctx.shared_runner if ctx.shared_runner is not None else ctx.runner, tree, build_digest(tree) or None,
        [str(c.get("cmd")) for c in ctx.checks if c.get("cmd")], gate_commands(ctx.landing_areas),
    )
    results, _, failures = invoke_graphs([inv], specs=ctx.specs, runner=view, run_id=run_id, max_parallel=1)
    for result in results:
        build_part = as_mapping(result.get("build"))
        change = str(build_part.get("patch") or "")
        combined = _compose_with_attempt(ctx, phase, tree, change) if change.strip() else None
        if build_part and combined is not None:
            result["build"] = {**build_part, "patch": combined, "resumed_from": attempt_sha, "change": change}
    return results, failures


PREVIOUS_OBJECTION_HEADING = "## Previous run's objection (recorded by the harness)"
PREVIOUS_OBJECTION_CHARS = 600


def previous_objection_block(result: Mapping[str, object] | None, cap: int = PREVIOUS_OBJECTION_CHARS) -> str:
    """The previous run's last adversary objection, else its last review finding, under a heading; '' with neither.

    `cap` bounds the text after the heading, not the heading itself.
    """
    if not isinstance(result, Mapping):
        return ""
    objections = [o for o in as_list(as_mapping(result.get("adversary")).get("objections")) if is_mapping(o)]
    findings = [f for f in as_list(as_mapping(result.get("review")).get("findings")) if is_mapping(f)]
    if objections:
        last = objections[-1]
        text = f"{last.get('claim') or ''} {last.get('why_wrong') or ''}".strip()
    elif findings:
        last = findings[-1]
        text = f"{last.get('detail') or ''} ({last.get('file')})" if last.get("file") else str(last.get("detail") or "")
    else:
        text = ""
    if not text.strip():
        return ""
    return f"{PREVIOUS_OBJECTION_HEADING}\n{text if len(text) <= cap else text[:cap] + '... truncated'}"


def permit_outside_surfaces(body: str, surfaces: Sequence[str], reason: str) -> tuple[str, list[str]]:
    """Body and surfaces with the files named by an outside-surfaces `reason` added, once each.

    Original surfaces keep their order and come first. A reason that names none returns both unchanged.
    """
    extra = tuple(f for f in dict.fromkeys(outside_surface_files(reason)) if f not in surfaces)
    if not extra:
        return body, list(surfaces)
    merged = [*surfaces, *extra]
    section = (
        "",
        "## Permitted surfaces (added by the harness)",
        "The failing tests lie outside the declared surfaces; they are now permitted:",
        *(f"- {s}" for s in merged),
    )
    return "\n".join((body, *section)), merged


def push_review_branch(ctx: Ctx, task: str) -> tuple[bool, str]:
    """Push the task's scratch branch to origin as `review/<initiative>/<task>`."""
    return _push_review_ref(ctx, ctx.scratch_branch(task), task)


def _push_review_ref(ctx: Ctx, local: str, name: str) -> tuple[bool, str]:
    """Push the local branch `local` to origin as `review/<initiative>/<name>`."""
    return git_in("-C", str(ctx.repo), "push", "origin", f"{local}:refs/heads/review/{ctx.initiative_id}/{name}")


def _landing_stamp() -> str:
    return landing_ops.stamp(datetime.now(UTC))


def _recorded_branch(record: Mapping[str, object]) -> str:
    """The landing record's branch; a record that has none raises `KeyError`."""
    return str(record["branch"])


def _initiative_branch_for(ctx: Ctx, initiative: Mapping[str, object]) -> str:
    """The initiative branch's name: the landing record's, else a `pr/<initiative>` an earlier release left, else the
    cartridge's template (default `init/<id>`). A template field the initiative lacks refuses the run by name."""
    conn = ctx.store.conn if ctx.store is not None else None
    record = landing_store.get_record(conn, ctx.initiative_id) if conn is not None else None
    if record is not None:
        return _recorded_branch(record)
    legacy = pr_branch_name(ctx.initiative_id)
    if landing_git.branch_ref(ctx.repo, legacy) is not None:
        return legacy
    try:
        return initiative_branch_name(ctx.cartridge, initiative, ctx.default_ref.removeprefix("origin/"))
    except TemplateError as exc:
        raise ValueError(f"{ctx.initiative_id}: {exc}") from exc


def start_initiative(ctx: Ctx, initiative: Mapping[str, object]) -> str:
    """Edge. Start the initiative's landing record on first use (cutting its branch from the parent) and return the
    branch name. A record that exists is kept as it is: the parent it names no longer floats."""
    name = _initiative_branch_for(ctx, initiative)
    # Another host may have cut it and merged phases into it: its copy on origin comes first, forward only.
    fetch_phase_branches(ctx, [name])
    if ctx.store is None:
        tip, _, why = landing_git.ensure_branch(ctx.repo, name, ctx.default_ref)
        if tip is None:
            raise RuntimeError(why)
        return name
    epic = initiative.get("epic")
    record, why = ctx.retry.write(
        lambda: landing_ops.start_record(
            ctx_store(ctx).conn, ctx.repo, initiative=ctx.initiative_id, branch=name,
            parent=ctx.default_ref.removeprefix("origin/"), parent_ref=ctx.default_ref, now=_landing_stamp(),
            land_at=ctx.land_at, epic=epic if isinstance(epic, str) else None,
        ),
        END_WRITE_WINDOW_S,
    )
    if record is None:
        raise RuntimeError(f"{ctx.initiative_id}: {why}")
    if resolve_push_initiative(ctx.cartridge) == "early":
        publish_branch(ctx, _recorded_branch(record))
    return _recorded_branch(record)


def _take_parent_forward(ctx: Ctx) -> tuple[str, str, landing_git.Merge] | None:
    """Edge. Bring the initiative branch up to its parent, under the branch lease: (parent, parent ref, what happened).

    The branch was cut from the parent and the parent has moved since: other initiatives landed on it. Before any phase
    builds, the parent is merged forward here when it merges cleanly, so no phase builds on an old base. A conflict, or
    a merge that could not be made, leaves the branch where it was. None without a store or an initiative branch."""
    if ctx.store is None or ctx.initiative_branch is None:
        return None
    branch = ctx.initiative_branch
    record = landing_store.get_record(ctx.store.conn, ctx.initiative_id) or {}
    # A stacked initiative's recorded parent is the branch of the initiative below; once that one landed, its branch is
    # gone and its own parent is the one to take forward.
    base = ctx.default_ref.removeprefix("origin/")
    stacked, unknown = landing_record.stack_resolved_parent(
        record, landing_store.list_records(ctx_store(ctx).conn), stacked=bool(record.get("epic")), epic_branch=base,
    ) if record else ("", None)
    parent = stacked or base
    parent_ref = f"origin/{parent}" if landing_git.rev(ctx.repo, f"refs/remotes/origin/{parent}") else parent
    if unknown is not None:
        # The layer below may be a row that does not read: whether it landed (and its branch is gone) is unknown, so
        # nothing is taken forward and no phase builds on a base nobody can name. `behind_parent` says so.
        return parent, parent_ref, landing_git.Merge("error", detail=(
            f"the landing record of {unknown.initiative}, which may be the layer below, cannot be read ({unknown.why})"))
    if landing_git.is_ancestor(ctx.repo, parent_ref, branch):
        return parent, parent_ref, landing_git.Merge("up_to_date")
    try:
        # Pushed whatever `push_initiative` says, as a phase merge is: a merge kept here is one no other host sees, and
        # the next merge made elsewhere would fork the branch.
        merged = ctx.retry.write(
            lambda: landing_ops.take_parent_forward(
                ctx_store(ctx).conn, ctx.repo, initiative=ctx.initiative_id, parent_ref=parent_ref,
                worker=f"run:{ctx.run_id}", now=_landing_stamp(), fetch=lambda b: _fetch_initiative(ctx, b),
                publish=lambda b: publish_branch(ctx, b),
            ),
            END_WRITE_WINDOW_S,
        )
    except Exception as exc:
        merged = landing_git.Merge("error", detail=f"{type(exc).__name__}: {exc}")
    if merged.status == "merged":
        print(f"  {branch} brought up to {parent} before any phase builds")
    return parent, parent_ref, merged


def behind_parent(
    status: str, detail: str, paths: Sequence[str], *, parent: str, branch: str, integrate: str | None, unfiled: str = ""
) -> str | None:
    """Pure. Why every phase but `integrate` waits this run, from the merge of the parent into the initiative branch;
    None when the branch holds its parent. `integrate` is the rectify task filed for a conflict, `unfiled` why none was."""
    if status in ("merged", "up_to_date", "busy"):
        return None  # `busy`: another worker is moving the branch right now; the next run takes the parent forward
    if status == "conflict":
        where = ", ".join(paths) or "files git did not name"
        then = (
            f"integrate task {integrate} resolves it first" if integrate
            else f"no integrate task could be filed ({unfiled or 'no reason was given'})"
        )
        return f"{branch} is behind {parent}, and merging {parent} into it conflicts in {where}; {then}, so no phase builds on the old base"
    return f"{parent} was not merged into {branch}: {detail or status}; no phase builds on the old base"


def _integrate_path(items: Sequence[Mapping[str, object]], task: str) -> Path | None:
    """The work file for a new integrate task, beside the initiative's others (`<work>/<initiative>/<phase>/<task>.md`);
    None when no task of it has a file."""
    found = next((Path(str(i["path"])) for i in items if i.get("path")), None)
    return found.parent.parent / INTEGRATE_PHASE / f"{task}.md" if found is not None else None


def _file_integrate(
    ctx: Ctx, items: list[EpicItem], parent: str, parent_ref: str, paths: Sequence[str]
) -> tuple[str | None, str]:
    """Edge. A ready rectify task in the `integrate` phase, as the lander files one: (its id, "") or (None, why not).

    The `integrate` branch is cut from the initiative tip and the conflicted merge of the parent is committed on it,
    markers and all, so the task builds on the conflict and nothing already built is redone. A rectify task of the
    phase still open is the one this conflict needs too: no second one is filed. The task goes to the store row, to
    a work file beside the others when they have files, and to this run's items, so this run builds it."""
    with ctx.items_lock:
        rows = [{"id": i["id"], "phase": i.get("phase"), "state": i.get("state")} for i in items]
    ident, still_open = landing_ops.rectify_task(INTEGRATE_PHASE, rows)
    if still_open:
        return ident, ""
    branch, integrate = str(ctx.initiative_branch), ctx.phase_branch(INTEGRATE_PHASE)
    tip, _, why = landing_git.ensure_branch(ctx.repo, integrate, branch)
    if tip is None:
        return None, why
    brought = landing_git.merge_into(
        ctx.repo, branch=integrate, source=branch, message=f"{ctx.initiative_id}: bring {integrate} up to {branch}",
    )
    if not brought.ok:
        return None, f"{integrate} could not be brought up to {branch}: {brought.detail or brought.status}"
    marked = landing_git.conflict_commit(
        ctx.repo, branch=integrate, source=parent_ref,
        message=f"{ctx.initiative_id}: merge {parent} into {integrate} (conflicted, to rectify)",
    )
    if marked.status == "error":
        return None, f"the integrate base could not be prepared: {marked.detail}"
    item: dict[str, object] = {
        "id": ident, "phase": INTEGRATE_PHASE, "state": "ready", "needs": [], "surfaces": [],
        "title": f"Integrate with {parent}: resolve the conflicts",
        "body": landing_ops.rectify_body(parent, INTEGRATE_PHASE, paths or marked.paths, ""),
    }
    path = _integrate_path(items, ident)
    try:
        from harness import store_queue  # not module level: see `mirror_read`

        if path is not None:
            workstore.write_item(item, path)
        store_queue.upsert(ctx_store(ctx).conn, {
            "kind": "task", "initiative": ctx.initiative_id, "task_id": ident, "phase": INTEGRATE_PHASE, "state": "ready",
            "needs": [], "surfaces": [], "title": item["title"], "body": item["body"], "extra": {"id": ident, "phase": INTEGRATE_PHASE},
            "updated_at": utc_now(), "updated_by": f"run:{ctx.run_id}",
        })
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    with ctx.items_lock:
        items.append(cast(EpicItem, {**item, **({"path": str(path)} if path is not None else {})}))
    return ident, ""


def bring_forward(ctx: Ctx, items: list[EpicItem]) -> str | None:
    """Edge. Take the parent forward before any phase; why every phase but `integrate` waits this run, else None."""
    forward = _take_parent_forward(ctx)
    if forward is None:
        return None
    parent, parent_ref, merged = forward
    integrate, unfiled = _file_integrate(ctx, items, parent, parent_ref, merged.paths) if merged.status == "conflict" else (None, "")
    reason = behind_parent(
        merged.status, merged.detail, merged.paths, parent=parent, branch=str(ctx.initiative_branch), integrate=integrate, unfiled=unfiled,
    )
    if reason is not None:
        print(f"  {reason}")
    elif merged.status == "busy":
        print(f"  {parent} not taken forward this run: {merged.detail}; the next run takes it forward")
    return reason


def _merge_checks(ctx: Ctx, phase: str) -> landing_git.Checks | None:
    """The level checks, to run on the merged tree when the merge joins the phase with work it has not seen."""
    checks = ctx.level_checks
    if not checks or landing_git.is_ancestor(ctx.repo, ctx.pr_branch(), ctx.phase_branch(phase)):
        return None  # the phase already holds the initiative tip: the merged tree is the tree its own checks passed on

    def run(tree: Path) -> tuple[bool, str]:
        with repo_gate_lock(ctx.repo):
            results = run_checks(tree, checks, env=checks_env(ctx, {}, phase, "@merge"))
        return (not (reason := level_failure_reason(phase, results))), reason

    return run


def unmerged_approved(rows: Sequence[Mapping[str, object]]) -> list[str]:
    """Pure. Ids of the approved tasks among `rows` that are not merged into their phase branch, in row order: the tasks
    that keep a phase from merging into the initiative branch. A task is merged when its row says so (`merged`, which
    the store reads as a recorded `merged_commit`); a task is never judged merged from branch ancestry here."""
    return [str(r["id"]) for r in rows if r.get("state") == "approved" and not r.get("merged")]


def merge_into_initiative(ctx: Ctx, phase: str, items: Sequence[Mapping[str, object]]) -> dict[str, object] | None:
    """Edge. Merge one built phase into the initiative branch, through the land queue's own operation.

    None when the phase is not built (a task is still open) or this run has no store. A conflict or a red check comes
    back as `{"status": "conflicted", ...}`: the phase stays built on its branch, its children do not start, and the
    lander files the rectify task. A failure of the store or of git is reported the same way and never stops the run:
    the queue item stays, and the lander merges the phase."""
    if ctx.initiative_branch is None:
        return None
    with ctx.items_lock:
        rows = [
            {"id": i["id"], "phase": i.get("phase"), "state": i.get("state"), "needs": as_list(i.get("needs")),
             "merged": bool(i.get("merged")), "merged_commit": i.get("merged_commit")}
            for i in items
        ]
    own = [r for r in rows if r["phase"] == phase]
    if ctx.store is None or not own or all(r["state"] in ("done", "dropped") for r in own):
        return None  # nothing of the phase is left to land
    # A phase is built when every task is approved (or dropped) and on the phase branch. Anything else is not whole,
    # so it is not merged, whatever the validator said of its goal: the phases that need it are cut from this branch.
    open_tasks = [str(r["id"]) for r in own if r["state"] not in ("approved", "done", "dropped")]
    loose = unmerged_approved(own)
    if open_tasks or loose:
        what = (f"task(s) {', '.join(open_tasks)} are not approved" if open_tasks
                else f"approved task(s) {', '.join(loose)} are not merged into {ctx.phase_branch(phase)}")
        return {"status": "not_built", "branch": ctx.initiative_branch, "assembled": False, "detail": what}
    try:
        # Always pushed, and before it is recorded, whatever `push_initiative` says: a merge this clone kept local is one
        # the lander cannot see, so it merged the phase a second time and the two copies of the branch diverged.
        done = ctx.retry.write(
            lambda: landing_ops.merge_phase(
                ctx_store(ctx).conn, ctx.repo, initiative=ctx.initiative_id, phase=phase, tasks=rows,
                worker=f"run:{ctx.run_id}", now=_landing_stamp(), checks=_merge_checks(ctx, phase),
                fetch=lambda branch: _fetch_initiative(ctx, branch),
                publish=lambda branch: publish_branch(ctx, branch),
            ),
            END_WRITE_WINDOW_S,
        )
    except Exception as exc:
        return {"status": "error", "branch": ctx.initiative_branch, "detail": f"{type(exc).__name__}: {exc}"}
    if done.status == "not_ready":
        return {"status": "not_built", "branch": ctx.initiative_branch, "assembled": False, "detail": done.detail}
    return {
        "status": "merged" if done.ok else done.status, "branch": ctx.initiative_branch, "tip": done.tip,
        "merge_sha": done.merge_sha, **({"paths": list(done.paths)} if done.paths else {}),
        **({"detail": done.detail} if done.detail else {}), **({"cause": done.cause} if done.cause else {}),
        "assembled": done.assembled,
    }


def _fetch_initiative(ctx: Ctx, branch: str) -> str | None:
    """Edge. Bring the initiative branch up to origin's copy, forward only. A reason when it must not be moved here:
    origin holds commits this clone lacks and they do not fast-forward, so a merge here would fork the branch."""
    outcome = fetch_initiative_branch(ctx, branch)
    if outcome.kind in (Kind.DIVERGED, Kind.DIVERGED_CONTENT):
        return f"{branch} has diverged from origin and is not merged into here: {outcome.detail}"
    return None


def fetch_initiative_branch(ctx: Ctx, branch: str) -> FetchOutcome:
    """Edge. This clone's initiative branch, brought up to origin's copy. Every merge into it is pushed before it is
    recorded, so origin's copy is the one: a copy here that diverged from it with the same tree (a second merge commit
    of the same work) is reset to origin's, and one with a different tree comes back DIVERGED_CONTENT, naming both
    commits, for the chair, who is told once (`tell_diverged`). A copy that is the same tree but whose origin copy
    lacks a commit the landing record names counts as different: the record may name this clone's merge commit."""
    outcome = fetch_phase_branches(ctx, [branch])[branch]
    if outcome.kind != Kind.DIVERGED:
        return outcome
    try:
        record = landing_store.get_record(ctx.store.conn, ctx.initiative_id) if ctx.store is not None else None
    except Exception as exc:
        return FetchOutcome(Kind.DIVERGED, f"{outcome.detail}; the landing record could not be read to settle it: {type(exc).__name__}: {exc}")
    settled = settle_diverged(ctx.repo, branch, landing_record.recorded_shas(record) if record else ())
    if settled.kind == Kind.RESET:
        print(f"  {settled.detail}")
    elif settled.kind == Kind.DIVERGED_CONTENT:
        tell_diverged(ctx, branch, settled.detail)
    return settled


LANE_DIVERGED_KEY = "lane_diverged:"  # the lander_notices key prefix of a lane's diverged initiative branch


def diverged_news(told: str | None, detail: str) -> bool:
    """Pure. Whether a lane's divergence is news for the chair: not told before with this same reason. The reason
    names both commits, so a copy that diverged again, or another host's copy, is told once more."""
    return told != detail


def tell_diverged(ctx: Ctx, branch: str, detail: str) -> None:
    """Edge. Raise needs_chair for this clone's diverged initiative branch, once per reason, as the lander does: the
    memory is the store's `lander_notices`, written only once the courier took the note. Best effort: a store or
    courier that fails leaves the phase's stop reason, which says the same, and the next run tries again."""
    if ctx.store is None:
        return
    key = f"{LANE_DIVERGED_KEY}{ctx.initiative_id}:{branch}"
    try:
        conn = ctx.store.conn
        if not diverged_news(landing_store.told(conn, key).get(key), detail):
            return
        if courier_adapter.send(f"coxswain://initiative/{ctx.initiative_id}", "chair", f"needs_chair: {ctx.initiative_id}: {detail}"):
            landing_store.remember(conn, key, detail, utc_now())
    except Exception as exc:
        print(f"  the chair was not told of the divergence of {branch}: {type(exc).__name__}: {exc}")


def parent_gap(ctx: Ctx, parent: str, items: Sequence[Mapping[str, object]]) -> tuple[str, str] | None:
    """Edge. Why the initiative branch in this clone does not hold parent phase `parent` yet; None when it does.

    (reason, the branch that is missing here or ""). The landing record says whether the phase merged, and git says
    whether this clone's branch holds that merge: a merge made on another host arrives by fetch, forward only. A
    parent whose every task is done has landed on the parent branch and is not asked about."""
    branch = str(ctx.initiative_branch)
    with ctx.items_lock:
        own = [str(i.get("state")) for i in items if i.get("phase") == parent]
    if own and all(s in ("done", "dropped") for s in own):
        return None
    try:
        record = landing_store.get_record(ctx.store.conn, ctx.initiative_id) if ctx.store is not None else None
    except Exception as exc:
        return f"the landing record of {ctx.initiative_id} could not be read: {type(exc).__name__}: {exc}", ""
    entry = ((record or {}).get("phases") or {}).get(parent) or {}
    if entry.get("state") != "merged":
        return f"parent phase '{parent}' is not merged into {branch} yet; dependents start once it is", ""
    sha = str(entry.get("merge_sha") or "")
    if sha and not landing_git.is_ancestor(ctx.repo, sha, branch):
        outcome = fetch_initiative_branch(ctx, branch)
        if outcome.kind == Kind.DIVERGED_CONTENT:
            return outcome.detail, ""
        if not landing_git.is_ancestor(ctx.repo, sha, branch):
            origin = f" ({outcome.kind.value}{': ' + outcome.detail if outcome.detail else ''})" if outcome else ""
            return (
                f"{branch} in this clone does not hold the merge of parent phase '{parent}' ({sha[:12]}), and origin "
                f"did not supply it{origin}; the phase would build without its parent's work", branch,
            )
    return None


def fetch_phase_branches(ctx: Ctx, branches: Sequence[str]) -> dict[str, FetchOutcome]:
    """Make each of `branches` a plain local branch where origin holds it; a branch already local only moves forward."""
    outcomes: dict[str, FetchOutcome] = {}
    for branch in branches:
        outcome = fetch_branch(ctx.repo, branch)
        if outcome.kind == Kind.FETCHED:
            ok, detail = materialise_local(ctx.repo, branch)
            outcome = outcome if ok else FetchOutcome(Kind.FETCH_FAILED, detail)
        outcomes[branch] = outcome
    return outcomes


def _await_launched_branch(ctx: Ctx, branch: str, outcome: FetchOutcome, *, patience: float = 60.0) -> FetchOutcome:
    """A branch missing everywhere while its phase is launched and unfinished is still being cut: wait for the cut, not the phase."""
    owner = ctx.launched.get(branch.removeprefix(f"epic/{ctx.initiative_id}/"))
    if outcome.kind != Kind.MISSING_EVERYWHERE or owner is None:
        return outcome
    deadline = time.monotonic() + patience
    while not owner.done() and not branch_exists(ctx, branch) and time.monotonic() < deadline:
        time.sleep(0.05)
    return fetch_branch(ctx.repo, branch)


def fetch_or_stop(
    ctx: Ctx,
    record: PhaseRecord,
    fetch: Sequence[str],
    required: Sequence[str],
    loose: Sequence[str] = (),
    parent_branches: Sequence[str] = (),
) -> PhaseRecord | None:
    """Fetch `fetch`; the stopped record when a branch the phase needs is missing everywhere or origin could not be asked.

    The needed branches are `required`, plus the parents' when a `loose` ref (a commit id) is not in this repo,
    since that commit can only come from a parent's branch. A parent nothing is needed from is fetched, never demanded.
    """
    outcomes = fetch_phase_branches(ctx, fetch)
    # A diverged branch kept its local tip; the record names origin's side ref so the divergence is never silent.
    diverged = [o.detail for o in outcomes.values() if o.kind == Kind.DIVERGED]
    if diverged:
        record["phase_branch_diverged"] = diverged
    unresolved = any(not git_in("-C", str(ctx.repo), "rev-parse", "--verify", "--quiet", f"{r}^{{commit}}")[0] for r in loose)
    demanded: list[str] = list(dict.fromkeys([*required, *(parent_branches if unresolved else [])]))
    outcomes = {b: _await_launched_branch(ctx, b, o) if b in demanded else o for b, o in outcomes.items()}
    for branch in demanded:
        if outcomes[branch].kind in (Kind.MISSING_EVERYWHERE, Kind.FETCH_FAILED):
            return missing_branch_phase(record, branch, missing_branch_cause(branch, outcomes[branch]))
    return None


def phase_branches_to_fetch(
    initiative_id: str, branch: str, parent_branches: Sequence[str], needed: Sequence[str], cut_from: Sequence[str]
) -> tuple[list[str], list[str], list[str]]:
    """`(fetch, required, loose)`: what to fetch, which of it the phase needs, and the needed refs that are not epic names.

    Fetched: the own branch, every parent's branch and the epic refs of needed and cut_from. Required: those epic refs
    only, so a parent nothing is needed from never stops the phase. Loose: the default ref, the pr branch, commit ids.
    """
    refs = list(dict.fromkeys([*needed, *cut_from]))
    required = [r for r in refs if r.startswith(f"epic/{initiative_id}/") and r != branch]
    loose = [r for r in refs if not r.startswith(f"epic/{initiative_id}/")]
    return list(dict.fromkeys([branch, *parent_branches, *required])), required, loose


def open_review_pr(ctx: Ctx, *, title: str, body: str, head: str, base: str) -> tuple[bool, str]:
    """Open a PR for `head` against `base`; the detail is its URL on success."""
    # A hung gh is exit 124 with `gh pr timed out after 120 seconds` as stderr, reported as any failed create.
    proc = bounded_proc.completed(
        ["gh", "pr", "create", "--title", title, "--body", body, "--head", head, "--base", base],
        _GH_TIMEOUT_SECONDS, cwd=ctx.repo, capture_output=True, text=True,
    )
    return proc.returncode == 0, (proc.stdout if proc.returncode == 0 else proc.stderr or proc.stdout).strip()


def _existing_pr(ctx: Ctx, head: str) -> tuple[bool, str]:
    """(known, url): known is False when the forge could not be asked, so "no PR" is never guessed from an error."""
    try:
        proc = bounded_proc.completed(
            ["gh", "pr", "view", head, "--json", "url", "--jq", ".url"],
            _GH_TIMEOUT_SECONDS, cwd=ctx.repo, capture_output=True, text=True,
        )
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode == 0:
        return True, proc.stdout.strip()
    if "no pull requests found" in proc.stderr.lower():
        return True, ""
    return False, (proc.stderr or proc.stdout).strip()


def open_grain_pr(
    ctx: Ctx, *, grain: str, name: str, local: str, title: str, base: str
) -> tuple[bool, str]:
    """Push `local` and open the PR for a `task` or `level` that is the configured granularity; the detail is the URL."""
    ok, detail = _push_review_ref(ctx, local, name)
    if not ok:
        return False, f"review branch push failed: {detail}"
    # A restart meets the PR the first run opened: reuse it, and open the one a failed first attempt never made.
    known, existing = _existing_pr(ctx, f"review/{ctx.initiative_id}/{name}")
    if not known:
        return False, f"could not check for an existing PR, so none was opened: {existing}"
    if existing:
        return True, existing
    ok, detail = open_review_pr(
        ctx, title=title, body=f"Landing granularity: {grain}", head=f"review/{ctx.initiative_id}/{name}", base=base
    )
    return ok, detail if ok else f"review PR failed: {detail}"


def default_branch(origin_head: str | None, local: set[str], head: str) -> str:
    """Origin's default if it is local, else main, else master, else `head`."""
    origin_name = (origin_head or "").removeprefix("origin/")
    if origin_name and origin_name in local:
        return origin_name
    if "main" in local:
        return "main"
    if "master" in local:
        return "master"
    return head


def origin_ref(base: str | None, origin_head: str | None, known: set[str], head: str) -> str:
    """`origin/<name>`: `base` when given (an `origin/` prefix dropped), else `default_branch` over `known`."""
    return f"origin/{(base or '').removeprefix('origin/') or default_branch(origin_head, known, head)}"


def resolve_default_ref(repo: Path, base: str | None) -> str:
    """Edge. `origin/<name>` after `git fetch origin`, whether or not origin has the branch; a failed fetch raises.

    The one bare local name is for a repo with no `origin` remote at all."""
    git = ("-C", str(repo))
    head_ok, head_out = git_in(*git, "rev-parse", "--abbrev-ref", "HEAD")
    origin_ok, origin_out = git_in(*git, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    local_ok, local_out = git_in(*git, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    remotes_ok, remotes = git_in(*git, "remote")
    has_origin = remotes_ok and "origin" in remotes.split()
    if has_origin:
        fetched, detail = git_in(*git, "fetch", "origin")
        if not fetched:
            raise RuntimeError(f"git fetch origin failed in {repo}: {detail[:200]}")
    tracked_ok, tracked_out = git_in(*git, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin")
    local_names = set(local_out.split()) if local_ok else set[str]()
    tracked_names = {name.removeprefix("origin/") for name in tracked_out.split()} if tracked_ok else set[str]()
    known = local_names | tracked_names
    ref = origin_ref(
        base,
        origin_out.strip() if origin_ok else None,
        known,
        head_out.strip() if head_ok else "HEAD",
    )
    return ref if has_origin else ref.removeprefix("origin/")


def level_failure_reason(phase: str, results: Sequence[Mapping[str, object]]) -> str:
    """A line naming the level and its first failing level check; '' when every row passed."""
    failing = next((r for r in results if not r.get("passed")), None)
    return "" if failing is None else f"level {phase} failed level check {failing.get('name')}"


def run_scratch(ctx: Ctx) -> Path:
    """The run's temp directory, under the system temp directory a hand run's pytest also uses, never under the home
    directory the worktrees sit in: a suite can read a path under a home directory as a home path."""
    return run_scratch_root(Path(gettempdir()), ctx.worktree_root / ctx.run_id)


def scratch_dir(ctx: Ctx, phase: str, task: str) -> Path:
    """`@` opens every name that is not a task id, so a task called `level` cannot share the level's directory."""
    return lane_scratch_dir(run_scratch(ctx), phase, task)


def checks_env(ctx: Ctx, lane: Mapping[str, str], phase: str, task: str) -> dict[str, str]:
    """The process environment with the worker count, the lane variable and a made temp directory (TMPDIR) over it."""
    scratch = scratch_dir(ctx, phase, task)
    scratch.mkdir(parents=True, exist_ok=True)
    workers = {} if ctx.lane_workers is None else workers_env(os.environ, ctx.lane_workers)
    return {**os.environ, **workers, **lane, **lane_tmp_env(scratch)}


class PhaseRunner:
    """One phase's view of the shared runner: its own repo_dir, repo_digest and check_commands, nothing assigned to the shared one.

    Every other attribute and call forwards to the shared runner, and its methods run with this view as `self`,
    so the three values read per phase while counters and flags stay shared. A runner that delegates through
    `_inner` gets a view of the inner runner too, and public names go there, as `_DelegatingFastPath` does.
    A name the shared runner lacks stays absent, as the duck-typed `hasattr` checks expect.
    """

    _shared: object
    _own: dict[str, object]
    _view: PhaseRunner | None

    def __init__(
        self,
        shared: object,
        repo_dir: Path,
        repo_digest: str | None,
        check_commands: list[str],
        gate_check_commands: dict[str, list[str]] | None = None,
    ) -> None:
        values = {
            "repo_dir": repo_dir,
            "repo_digest": repo_digest,
            "check_commands": check_commands,
            "gate_check_commands": gate_check_commands or {},
        }
        inner = vars(shared).get("_inner") if hasattr(shared, "__dict__") else None
        view = None if inner is None else PhaseRunner(inner, repo_dir, repo_digest, check_commands, gate_check_commands)
        own = {} if inner is not None else {n: v for n, v in values.items() if hasattr(shared, n)}
        object.__setattr__(self, "_shared", shared)
        object.__setattr__(self, "_own", own)
        object.__setattr__(self, "_view", view)

    @property
    def __class__(self) -> type[object]:  # so `super()` and `isinstance` inside the shared runner's methods accept this view
        return type(self._shared)

    @__class__.setter
    def __class__(self, value: type[object]) -> None:
        """A view's class is the shared runner's, read-only: `object.__setattr__(view, "__class__", X)` reaches here,
        past `__setattr__`, and must not retype the shared runner under every other phase."""
        raise TypeError(f"a PhaseRunner view's __class__ is read-only (asked to become {value.__name__})")

    def __getattr__(self, name: str) -> object:
        if name in ("_shared", "_own", "_view"):
            raise AttributeError(name)
        if name == "_inner" and self._view is not None:
            return self._view
        if name in self._own:
            return self._own[name]
        if self._view is not None and not name.startswith("_"):
            return getattr(self._view, name)
        value = getattr(self._shared, name)
        if isinstance(value, types.MethodType) and value.__self__ is self._shared:
            return types.MethodType(value.__func__, self)
        return value

    def __setattr__(self, name: str, value: object) -> None:
        if name in self._own:
            self._own[name] = value
        elif self._view is not None and not name.startswith("_"):
            setattr(self._view, name, value)
        else:
            setattr(self._shared, name, value)


def missing_branch_phase(record: PhaseRecord, branch: str, cause: str) -> PhaseRecord:
    """Block the phase on a branch it needs and cannot find. Nothing is quarantined: the run stops, the work is untouched."""
    record["status"] = "blocked"
    record["reason"] = cause
    record["missing_branch"] = branch
    return record
