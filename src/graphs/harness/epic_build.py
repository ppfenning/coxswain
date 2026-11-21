"""The epic driver, building a task: the lane and level checks, the lifecycle invocation, the style and check fixes."""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import TypedDict

from core import workstore
from core.jsontypes import is_dict, is_mapping
from graphs.delivery.budget_estimate import estimate_budget
from graphs.delivery.lifecycle_propose import DEFAULT_FIX_ATTEMPTS
from harness import pool_edge, warm_pool
from harness.check_deps import load_dep_map
from harness.check_gate import repo_gate_lock
from harness.check_reuse import run_checks_reusing
from harness.check_scope import is_test_file, pytest_component, render_selection, select_checks
from harness.checks import (
    check_feedback,
    checks_evidence,
    collected_ids,
    coverage_floor_holds,
    fixable_checks,
    is_harness_fault,
    quarantine_reason,
    refix_route,
    run_checks,
)
from harness.epic_branches import (
    CHECK_FIX_PROMPT,
    IDENTITY,
    LIFECYCLE,
    PATCH_FOR_VALIDATION_CHARS,
    STYLE_PASS_SCHEMA,
    attempt_tree,
    carry_forward,
    checks_env,
    invoke_from_attempt,
    level_failure_reason,
    permit_outside_surfaces,
    resume_body,
    scratch_dir,
)
from harness.epic_context import Ctx, git_in
from harness.epic_phase_branch import trace_evidence
from harness.epic_records import epic_log, record_steps, save_task_result, wait_if_paused_here
from harness.epic_types import BuildRecord, PhaseRecord, as_list, as_mapping
from harness.escalate import touched_paths
from harness.integration_scope import scope_integration
from harness.invoke import Invocation, invoke_graphs
from harness.lane_database import database_name, lane_env
from harness.lane_database_create import create_lane_database, creation_failure
from harness.lane_database_drop import drop_lane_database
from harness.store_steps import with_checks
from harness.worktree import apply_patch, create_worktree, keep_worktree, link_venv, read_lock, remove_worktree


class _ChecksLog(TypedDict):
    """The `run_checks` arguments that say where a run's check output is logged."""

    log_dir: Path
    log_prefix: str


def _as_int(value: object) -> int:
    """`value` when it is an int, else 0: a count a result left out."""
    return value if isinstance(value, int) else 0


def _as_float(value: object) -> float:
    """`value` as a float when it is a number or a numeral, else 0.0."""
    return float(value) if isinstance(value, int | float | str) else 0.0


def _checks_log(ctx: Ctx, phase: str, task: str) -> _ChecksLog:
    """`run_checks` log arguments: `runs/<run>/checks/<phase>/<task>/<n>.log`, so tasks never overwrite each other."""
    return {"log_dir": ctx.runs_dir / ctx.run_id / "checks" / phase / task, "log_prefix": f"checks/{phase}/{task}/"}


def _ledger_dir(worktree: Path) -> Path | None:
    """The repository's git common directory, shared by every worktree; None when git cannot say."""
    ok, out = git_in("-C", str(worktree), "rev-parse", "--git-common-dir")
    return (worktree / out).resolve() if ok and out else None


def _gate_checks(
    worktree: Path,
    checks: Sequence[Mapping[str, object]],
    *,
    env: Mapping[str, str] | None = None,
    log_dir: Path | None = None,
    log_prefix: str = "",
) -> list[dict[str, object]]:
    """`run_checks` for a task gate or style pass: a pass on this tree and command is reused and recorded."""
    ledger = _ledger_dir(worktree)
    if ledger is None:
        return list(run_checks(worktree, checks, env=env, log_dir=log_dir, log_prefix=log_prefix))
    return list(
        run_checks_reusing(
            worktree, checks, ledger_dir=ledger, run=run_checks, env=env, log_dir=log_dir, log_prefix=log_prefix
        )
    )


def _reused_names(results: Sequence[Mapping[str, object]]) -> list[str]:
    return [str(r.get("name")) for r in results if r.get("reused")]


def _nul_paths(listing: str) -> list[str]:
    return [p for p in listing.split("\0") if p]


def _lane_checks(
    worktree: Path, base: str, checks: Sequence[Mapping[str, object]]
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """The checks the task's patch can affect, and the selection; every check, with a reason, on any error."""
    known = [c for c in (pytest_component(str(check.get("cmd", ""))) for check in checks) if c]

    def everything(why: str) -> tuple[list[dict[str, object]], dict[str, object]]:
        return [dict(c) for c in checks], {
            "components_run": known,
            "components_skipped": [],
            "narrowed": dict.fromkeys(known, "whole"),
            "added_tests": [],
            "reason": dict.fromkeys(known or ["*"], f"fallback {why}"),
        }

    ok, changed = git_in("-C", str(worktree), "diff", "--name-only", "--no-renames", "-z", base, "HEAD")
    added_ok, added = git_in(
        "-C", str(worktree), "diff", "--name-only", "--no-renames", "-z", "--diff-filter=A", base, "HEAD"
    )
    if not (ok and added_ok):
        return everything(f"diff unreadable: {changed if not ok else added}")
    try:
        dep_map = load_dep_map(worktree, known)
        import_map = {
            "components": dep_map.components,
            "test_reach": dep_map.test_reach,
            "unresolved": dep_map.unresolved,
        }
        added_tests = [p for p in _nul_paths(added) if is_test_file(p)]
        scoped = select_checks(checks, _nul_paths(changed), added_tests, import_map)
    except Exception as exc:  # fail open: whatever broke, the full list still runs
        return everything(f"import map failed: {type(exc).__name__}: {exc}")
    return list(scoped.checks), scoped.selection


def _tests_run(results: Sequence[Mapping[str, object]]) -> int:
    return sum(_as_int(as_mapping(r.get("counts")).get(k, 0)) for r in results for k in ("passed", "failed", "error"))


def _cpu_seconds_total(results: Sequence[Mapping[str, object]]) -> float:
    return round(sum(_as_float(r.get("cpu_seconds", 0.0)) for r in results), 3)


def _scope_figures(selection: Mapping[str, object], results: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Per-component counts and their totals under the dash feed's key names; {} when the selection has no counts."""
    tests = selection.get("tests")
    if not tests:
        return {}
    per_component = as_mapping(tests).values()
    return {
        "tests": tests,
        "totals": {
            "tests_selected": sum(_as_int(as_mapping(t)["selected"]) for t in per_component),
            "tests_total": sum(_as_int(as_mapping(t)["total"]) for t in per_component),
            "cpu_seconds": _cpu_seconds_total(results),
        },
    }


def _scope_clause(figures: Mapping[str, object]) -> str:
    totals = as_mapping(figures.get("totals"))
    if not totals:
        return ""
    return f"; tests {totals['tests_selected']} of {totals['tests_total']}, cpu {totals['cpu_seconds']:.1f}s"


def _scope_record(selection: Mapping[str, object], results: Sequence[Mapping[str, object]]) -> tuple[dict[str, object], str]:
    """The `check_scope` record and the `checks:scope` row text for one run of the scoped checks."""
    tests_run, figures = _tests_run(results), _scope_figures(selection, results)
    row = f"{render_selection(selection)}; tests_run={tests_run}{_scope_clause(figures)}"
    return {**selection, "tests_run": tests_run, **figures}, row


def run_level_checks(ctx: Ctx, phase: str, record: PhaseRecord) -> None:
    """Run the level checks once in the level's worktree, under the repo gate lock, and fold the result into `record`.

    No level checks configured: nothing runs and `record` gains no key. A failure marks the level, never a task.
    """
    checks = ctx.level_checks
    if not checks:
        return
    log = _ChecksLog(log_dir=ctx.runs_dir / ctx.run_id / "checks" / phase / "level", log_prefix=f"checks/{phase}/level/")
    with repo_gate_lock(ctx.repo):
        results = run_checks(ctx.phase_worktree(phase), checks, env=checks_env(ctx, {}, phase, "@level"), **log)
    record["level_checks"] = results
    if reason := level_failure_reason(phase, results):
        record["status"], record["reason"] = "partial", reason


def _level_integration_reason(phase: str, creation: Mapping[str, str], results: Sequence[Mapping[str, object]]) -> str:
    """A line naming the level and what stopped its integration gate; '' when the database exists and every row passed."""
    if setup := creation_failure(creation):
        return f"level {phase}: {setup}"
    failing = next((r for r in results if not r.get("passed")), None)
    return "" if failing is None else f"level {phase} failed integration check {failing.get('name')}"


def run_level_integration(ctx: Ctx, phase: str, base: str, record: PhaseRecord) -> None:
    """Run the touched-package integration checks once on the level's own database, after the level checks.

    `base` is the ref the level was cut from. Nothing runs, and `record` gains no key, without both
    `integration_checks` and a `test_database`. The database is registered before it is created, so a
    failed creation is still listed by the drop path. A failure marks the level partial, never a task.
    """
    checks, setting = ctx.integration_checks, ctx.test_database
    if not checks or setting is None:
        return
    worktree = ctx.phase_worktree(phase)
    lane = lane_env(setting, ctx.run_id, phase, "level")
    name = ctx.lane_names[(phase, "level")] = database_name(next(iter(lane.values())))
    creation = create_lane_database(setting.get("create"), setting.get("schema", ""), name, lane, str(worktree))
    ok, listing = git_in("-C", str(worktree), "diff", "--name-only", "--no-renames", "-z", base, "HEAD")
    if not ok:
        record["status"], record["reason"] = "partial", f"level {phase} integration paths unreadable: {listing}"
        return
    scoped = scope_integration(checks, _nul_paths(listing))
    results: list[dict[str, object]] = []
    if scoped.checks and not creation_failure(creation):
        log = _ChecksLog(
            log_dir=ctx.runs_dir / ctx.run_id / "checks" / phase / "level-integration",
            log_prefix=f"checks/{phase}/level-integration/",
        )
        with repo_gate_lock(ctx.repo):
            results = run_checks(worktree, list(scoped.checks), env=checks_env(ctx, lane, phase, "@level-integration"), **log)
    record["level_integration"], record["level_integration_scope"] = results, scoped.selection
    if reason := _level_integration_reason(phase, creation, results):
        record["status"], record["reason"] = "partial", reason


def _remove_scratch(ctx: Ctx, phase: str, task: str) -> None:
    shutil.rmtree(scratch_dir(ctx, phase, task), ignore_errors=True)


# The branch a lane's checks diff against: its phase branch, so the unwired check (.github/check_unwired.py)
# judges this task's patch, not every earlier phase and merged sibling on the way back to main.
LANE_BASE_VAR = "COX_LANE_BASE"


def _lane_checks_env(ctx: Ctx, phase: str, task: str) -> dict[str, str]:
    """The child environment for one lane's checks: its database, worker count, own temp directory and base branch."""
    lane = {**lane_env(ctx.test_database, ctx.run_id, phase, task), LANE_BASE_VAR: ctx.phase_branch(phase)}
    return checks_env(ctx, lane, phase, task)


def drop_lane(ctx: Ctx, phase: str, task: str) -> None:
    """Remove the lane's scratch directory, then drop its database once its worktree is gone and list the outcome."""
    _remove_scratch(ctx, phase, task)
    name = ctx.lane_names.pop((phase, task), None)
    if name is None or ctx.test_database is None:
        return
    ctx.lane_drops.append(
        {**drop_lane_database(ctx.test_database.get("drop"), name, lane_env(ctx.test_database, ctx.run_id, phase, task))}
    )


def _lease_task(ctx: Ctx, *, phase: str, task: str, branch: str) -> tuple[bool, str, dict[str, object]]:
    """Edge: lease the task's worktree from the pool. Returns (ok, failure detail, setup record)."""
    pool = ctx.pool
    assert pool is not None
    try:
        lease = pool_edge.acquire(
            ctx.repo,
            pool.pool_root,
            branch=branch,
            base=ctx.phase_branch(phase),
            sync_argv=pool.sync_argv,
            lock_names=pool.lock_names,
            keep=warm_pool.pool_size(ctx.max_parallel),
            clock=pool.clock,
        )
    except pool_edge.PoolError as exc:
        return False, str(exc), {}
    digest = warm_pool.lock_digest(read_lock(lease.path, pool.lock_names))
    ctx.pool_leases[(phase, task)] = (lease, digest)
    return True, "", warm_pool.setup_record(lease.mode, lease.setup_seconds)


def _release_task(ctx: Ctx, phase: str, task: str) -> None:
    """Give back the task's pooled lease, or remove its worktree when it holds none."""
    held = ctx.pool_leases.pop((phase, task), None)
    if held is None:
        remove_worktree(ctx.repo, ctx.task_worktree(phase, task))
    else:
        pool_edge.release(*held)


def settle_pool(ctx: Ctx, *, keep_worktrees: bool) -> None:
    """Edge, at epic end: release every lease still held (or move it out of the pool when kept), then trim.

    A kept worktree leaves its directory, so its index entry is dropped by trim's vanished-directory pass.
    """
    pool = ctx.pool
    if pool is None:
        return
    held = sorted(ctx.pool_leases.items())
    ctx.pool_leases.clear()
    for (phase, task), (lease, digest) in held:
        try:
            if keep_worktrees:
                keep_worktree(ctx.repo, lease.path, ctx.worktree_root, ctx.run_id)
            else:
                pool_edge.release(lease, digest)
        except Exception as exc:
            epic_log.warning("pooled worktree for %s/%s not settled: %s: %s", phase, task, type(exc).__name__, exc)
    try:
        pool_edge.trim(ctx.repo, pool.pool_root, pool.keep, pool.clock)
    except Exception as exc:
        epic_log.warning("pool trim for %s failed: %s: %s", ctx.run_id, type(exc).__name__, exc)


def build_task(
    ctx: Ctx, *, phase: str, task: str, result: Mapping[str, object], verify: Sequence[str] = ()
) -> BuildRecord:
    """Build and check one task; its pytest scratch directory is removed as soon as its checks have run."""
    try:
        return _build_task_checked(ctx, phase=phase, task=task, result=result, verify=verify)
    finally:
        _remove_scratch(ctx, phase, task)


def _build_task_checked(
    ctx: Ctx, *, phase: str, task: str, result: Mapping[str, object], verify: Sequence[str] = ()
) -> BuildRecord:
    """Apply one task's patch on a scratch branch off the phase branch, and check it.

    Returns the record whose evidence rows the gate will read. Whether the tests
    pass is not something a review node gets to assert, and a task whose checks
    fail has not met its done criteria — done criteria consume machine evidence,
    not claims — so a failure here quarantines WITH the evidence attached rather
    than proposing a merge and hoping.
    """
    patch = str(as_mapping(result.get("build")).get("patch") or "")
    worktree = ctx.task_worktree(phase, task)
    branch = ctx.scratch_branch(task)
    record: BuildRecord = {"id": task, "phase": phase, "branch": branch, "evidence": []}

    started = monotonic()
    if ctx.pool is None:
        ok, detail = create_worktree(ctx.repo, worktree, branch=branch, base=ctx.phase_branch(phase))
        if ok:
            link_venv(ctx.repo, worktree)
        setup = warm_pool.setup_record("cold", monotonic() - started)
    else:
        ok, detail, setup = _lease_task(ctx, phase=phase, task=task, branch=branch)
        worktree = ctx.task_worktree(phase, task)
    if not ok:
        record["quarantine"] = f"worktree {branch} could not be created: {detail}"
        return record
    record["setup"] = setup
    env = _lane_checks_env(ctx, phase, task)
    lane = lane_env(ctx.test_database, ctx.run_id, phase, task)
    if lane:
        record["test_database"] = ctx.lane_names[(phase, task)] = database_name(next(iter(lane.values())))

    ok, detail = apply_patch(patch, worktree)
    record["evidence"].append(
        {"check": "patch_apply", "output": f"ok — applied in {worktree}" if ok else f"FAIL — {detail}"}
    )
    if not ok:
        record["quarantine"] = f"patch did not apply: {detail}"
        return record
    record["evidence"].extend(dict(row) for row in trace_evidence(getattr(ctx.runner, "calls", None) or [], task, patch))

    # Commit BEFORE the checks run, so the branch holds exactly the applied
    # patch and nothing else. Checks execute things — a test run drops
    # __pycache__ and friends into the worktree, and an add -A afterwards would
    # commit those byproducts, which then differ per task and collide as binary
    # conflicts at merge time. Found by running the whole driver against a real
    # repository, which is the only place a bug like this can live.
    ok, detail = git_in(*IDENTITY, "-C", str(worktree), "add", "-A")
    if ok:
        ok, detail = git_in(
            *IDENTITY, "-C", str(worktree), "commit", "--allow-empty", "-q",
            "-m", f"epic {ctx.run_id}: {task}",
        )
    if not ok:
        record["quarantine"] = f"the applied patch could not be committed: {detail}"
        return record

    # Mechanical, model-free: a lint fix is not a build attempt, so it runs
    # here rather than looping the fix back through review. Its changes are
    # amended into the task's own commit, so the branch stays one commit (the
    # land takes exactly one), and get folded into `result`'s own patch so
    # every later reader — validation, escalation, merge — sees the fixed
    # file, not the one the build actually produced.
    fixable = fixable_checks(ctx.checks)
    record["lint_fix_checks"] = [c["name"] for c in fixable]
    record["lint_fixed"] = False
    if fixable:
        run_checks(worktree, fixable, env=env)
        _, dirty = git_in("-C", str(worktree), "status", "--porcelain")
        if dirty:
            git_in(*IDENTITY, "-C", str(worktree), "add", "-A")
            ok, detail = git_in(*IDENTITY, "-C", str(worktree), "commit", "-q", "--amend", "--no-edit")
            if ok:
                record["lint_fixed"] = True
                build_field = result.get("build")
                _, folded = git_in("-C", str(worktree), "diff", ctx.phase_branch(phase), "HEAD")
                if is_dict(build_field) and folded:
                    build_field["patch"] = folded + "\n"

    if ctx.checks:
        scoped, selection = _lane_checks(worktree, ctx.phase_branch(phase), ctx.checks)
        results = _gate_checks(worktree, scoped, env=env, **_checks_log(ctx, phase, task))
        record["checks"] = results
        record["evidence"].extend(dict(row) for row in checks_evidence(results))
        scope, row = _scope_record(selection, results)
        scope["reused"] = _reused_names(results)
        record["check_scope"] = scope
        record["check_cpu_seconds"] = _cpu_seconds_total(results)
        record["evidence"].append({"check": "checks:scope", "output": row})
        reason = quarantine_reason(results)
        if reason:
            record["quarantine"] = reason

    # The ticket's own verification commands, run here because the builder's
    # sandbox cannot. Their output is evidence for the reviewers; a failing one
    # never quarantines, so its results stay out of `quarantine_reason`.
    if verify:
        ran = run_checks(worktree, [{"name": str(i), "cmd": cmd} for i, cmd in enumerate(verify, 1)], env=env)
        record["verify"] = ran
        record["evidence"].extend(dict(row) for row in checks_evidence(ran, prefix="verify", always_tail=True))
    return record


def task_checks(
    ctx: Ctx, worktree: Path, *, phase: str, label: str, verify: Sequence[str] = ()
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """The task gate's cheap checks, narrowed to what `worktree`'s HEAD changes against `ctx.phase_branch(phase)`, and
    a task's `verify` commands, run there in the lane environment of `label` (its scratch, log and database name; an
    `@`-name, so never a task id's): (check results, verify results). How the phase lane's merge agent checks each
    colliding task on the merged tree (`phase_lane._collide`). Its scratch and any lane database are dropped after."""
    lane = lane_env(ctx.test_database, ctx.run_id, phase, label)
    if lane:
        ctx.lane_names[(phase, label)] = database_name(next(iter(lane.values())))
    try:
        env = _lane_checks_env(ctx, phase, label)
        scoped = _lane_checks(worktree, ctx.phase_branch(phase), ctx.checks)[0] if ctx.checks else []
        results = _gate_checks(worktree, scoped, env=env, **_checks_log(ctx, phase, label)) if scoped else []
        commands = [{"name": f"verify-{i}", "cmd": cmd} for i, cmd in enumerate(verify, 1)]
        return results, list(run_checks(worktree, commands, env=env)) if commands else []
    finally:
        drop_lane(ctx, phase, label)


def verify_of(by_id: Mapping[str, Mapping[str, object]], task: str) -> list[str]:
    return [str(c) for c in as_list(as_mapping(by_id.get(task)).get("verify"))]


_BUDGET_HISTORY_DAYS = 30


def _budget_history(ctx: Ctx) -> list[tuple[str, int, float]]:
    """The last 30 days of landed build-task cost history for this run's repo, empty with no store.

    `ctx.date` bounds the window, never the wall clock, so an estimate stays
    reproducible from the run's own recorded inputs rather than from when it
    happened to run. Both `ctx.store` and `ctx.repo` are read with `getattr`:
    `lifecycle_invocation` is also called, in its own unit test, against a
    bare stand-in object that carries neither, and that case gets no history
    rather than an `AttributeError`.
    """
    store = getattr(ctx, "store", None)
    if store is None:
        return []
    # Not a module-level import: harness/__init__ imports this module, so a
    # top-level store_read import would pull in store_traces earlier than
    # `python -m harness.store_traces` expects, and it warns a second line on
    # stderr when that happens.
    from harness import store_read

    since_date = datetime.fromisoformat(ctx.date).date() - timedelta(days=_BUDGET_HISTORY_DAYS)
    since = f"{since_date.isoformat()}T00:00:00+00:00"
    return store_read.build_task_history(store.conn, str(getattr(ctx, "repo", "")), since)


def lifecycle_invocation(
    ctx: Ctx, task: Mapping[str, object], *, body: str, fix_attempts: int | None
) -> Invocation:
    # Every build task resolves a budget and where it came from, never
    # nothing. A ticket's own `budget_usd` wins outright and is checked
    # against the cartridge's `build_budget_usd_max` cap further up in
    # `run_phase` (the `over_budget`/`runnable` split), before this function
    # is ever called, and never through `quarantine_task` here. An estimate
    # is already clamped to [1.00, 4.00], so it can never reach that cap or
    # the quarantine path either.
    build_budget_usd: object
    if task.get("budget_usd") is not None:
        build_budget_usd, build_budget_source = task["budget_usd"], "ticket"
    else:
        build_budget_usd, _source = estimate_budget(
            _budget_history(ctx), str(getattr(ctx, "repo", "")), len(as_list(task.get("surfaces")))
        )
        build_budget_source = "estimate"
    return Invocation(
        id=str(task["id"]),
        graph=LIFECYCLE,
        args={
            "date": ctx.date,
            "ticket": task["id"],
            "work_item": True,
            "ticket_title": task.get("title") or "",
            "ticket_body": body,
            "cartridge": ctx.cartridge,
            "surfaces": as_list(task.get("surfaces")),
            "patterns": as_list(task.get("patterns")),
            "tier": dict(as_mapping(task.get("tier"))),
            **({"fix_attempts": fix_attempts} if fix_attempts is not None else {}),
            "build_budget_usd": build_budget_usd,
            "build_budget_source": build_budget_source,
        },
    )


def _style_fix(ctx: Ctx, *, phase: str, task: str, result: Mapping[str, object], build: BuildRecord) -> bool:
    """One `style_pass` over an approved build whose lint failed, then the checks again.

    The seat is a model and no reviewer sees its edit, so it is bounded three
    ways. Its patch passes the coverage floor `trim_phase` uses, no collected
    test id may disappear. Only the paths it names are staged, because the
    checks have already written byproducts into this worktree. And the edit is
    recorded as an evidence row, for whoever reads the gate. True when the patch
    applied, held the floor and was committed; `build` then carries the fresh
    check results and `result`'s patch the folded diff. False restores the
    worktree, for a fall back to a revise.
    """
    worktree = ctx.task_worktree(phase, task)
    before = collected_test_ids(worktree)
    if before is None:
        return False  # no floor can be measured, so no model call is worth buying
    patch = str(as_mapping(result.get("build")).get("patch") or "")
    if "checks" not in build:
        raise KeyError("checks")
    edit = ctx.runner.run(
        role="style_pass",
        tier="standard",
        schema=STYLE_PASS_SCHEMA,
        task=task,
        wait_if_paused=lambda: wait_if_paused_here(ctx),
        context=as_list(ctx.cartridge.get("context")),
        prompt=(
            f"{CHECK_FIX_PROMPT}\n\nTicket: {task}\n\nFailing checks:\n{check_feedback(build['checks'])}"
            f"\n\nApproved diff:\n{patch[:PATCH_FOR_VALIDATION_CHARS]}"
        ),
    ).get("patch")
    if not str(edit or "").strip():
        return False
    if not apply_patch(str(edit), worktree)[0]:
        return False
    after = collected_test_ids(worktree)
    if after is None or not coverage_floor_holds(before, after):
        git_in("-C", str(worktree), "reset", "--hard", "-q")
        git_in("-C", str(worktree), "clean", "-fdq")
        build["evidence"].append({"check": "style_pass", "output": "refused (coverage floor)"})
        return False
    paths = touched_paths(str(edit))
    staged, _ = git_in(*IDENTITY, "-C", str(worktree), "add", "-A", "--", *paths)
    if not staged or not git_in(*IDENTITY, "-C", str(worktree), "commit", "-q", "-m", "style pass")[0]:
        return False
    _, folded = git_in("-C", str(worktree), "diff", ctx.phase_branch(phase), "HEAD")
    build_field = result.get("build")
    if is_dict(build_field) and folded:
        build_field["patch"] = folded + "\n"
    scoped, selection = _lane_checks(worktree, ctx.phase_branch(phase), ctx.checks)
    try:
        results = _gate_checks(worktree, scoped, env=_lane_checks_env(ctx, phase, task), **_checks_log(ctx, phase, task))
    finally:
        _remove_scratch(ctx, phase, task)
    build["checks"] = results
    scope, scope_row = _scope_record(selection, results)
    scope["reused"] = _reused_names(results)
    build["check_scope"] = scope
    build["check_cpu_seconds"] = _cpu_seconds_total(results)
    # Only `checks:` rows are replaced; `verify:` rows stay as run before the style edit.
    build["evidence"] = [
        *(row for row in build["evidence"] if not str(row.get("check")).startswith("checks:")),
        {"check": "style_pass", "output": f"edit applied after approval, not reviewed: {', '.join(paths)}"},
        *(dict(row) for row in checks_evidence(results)),
        {"check": "checks:scope", "output": f"style pass: {scope_row}"},
    ]
    build["quarantine"] = quarantine_reason(results)
    return True


def refix_task(
    ctx: Ctx,
    by_id: Mapping[str, Mapping[str, object]],
    *,
    phase: str,
    task: str,
    result: dict[str, object],
    build: BuildRecord,
) -> tuple[dict[str, object], BuildRecord]:
    """Send an approved build whose checks failed back through the fix loop, while attempts remain.

    The loop approved this build, and a configured check then failed on it: a
    one-token lint error cost a whole rerun when that was quarantined at once.
    The check's own output goes back to the builder, either through the
    `style_pass` seat for a lint-only failure or as a fresh lifecycle run whose
    ticket body carries it, and the checks run again on what comes back.
    Attempts are the loop's own budget, `fix_attempts` plus the first build,
    less what the loop already spent and what each re-entry spends. Once none
    remain, or a re-entry is not approved, the build quarantines as before.
    A task gets at most one `style_pass` edit; a revise is a whole reviewed
    lifecycle run, and each one leaves an evidence row saying what it spent.
    """
    limit = DEFAULT_FIX_ATTEMPTS if ctx.fix_attempts is None else ctx.fix_attempts
    spent = _as_float(as_mapping(result.get("fix_loop")).get("attempts") or 1)
    item = {**as_mapping(by_id.get(task)), "id": task}
    routes: list[str] = []
    while build.get("quarantine") and "checks" in build:
        left = limit + 1 - spent
        # One unreviewed style edit per task: a second failure goes to a reviewed revise.
        style_open = "style_pass" in ctx.bound and "style_pass" not in routes
        route = refix_route(build["checks"], ctx.checks, attempts_left=left, style_bound=style_open)
        if route == "quarantine":
            break
        before = list(build["checks"])
        if route == "style_pass" and _style_fix(ctx, phase=phase, task=task, result=result, build=build):
            # The checks re-run on the edit take this attempt's `checks` key; the ones that sent it to the edit are kept.
            record_steps(ctx, with_checks(result, before, ctx.run_id, stage="prestyle"), phase=phase, task=task)
            spent += 1
            routes.append("style_pass")
            continue
        failed = {
            "run": ctx.run_id,
            "phase": phase,
            "reason": f"approved, then failed its configured checks:\n{check_feedback(build['checks'])}",
        }
        # The checks that send this attempt back are its own step, stored under it as the re-entry's are under it.
        record_steps(ctx, with_checks(result, build["checks"], ctx.run_id), phase=phase, task=task)
        last_patch = str(as_mapping(result.get("build")).get("patch") or "")
        history = [*(a for a in as_list(item.get("attempts")) if is_mapping(a)), failed]
        # The revise starts from the patch that just failed its checks, in a tree of its own, not from a reference.
        attempt = attempt_tree(ctx, phase, f"{task}--refix{len(routes) + 1}", last_patch) if last_patch.strip() else None
        carried = (
            resume_body(str(item.get("body") or ""), history, attempt[1])
            if attempt is not None
            else carry_forward(str(item.get("body") or ""), history, last_patch, limit=PATCH_FOR_VALIDATION_CHARS)
        )
        body, permitted = permit_outside_surfaces(
            carried, [str(s) for s in as_list(item.get("surfaces"))], str(build.get("quarantine") or "")
        )
        refix = lifecycle_invocation(ctx, {**item, "surfaces": permitted}, body=body, fix_attempts=max(int(left) - 1, 0))
        refix_run = f"{ctx.run_id}:{phase}:refix{len(routes) + 1}"
        if attempt is not None:
            retried, failures = invoke_from_attempt(ctx, phase, refix, *attempt, run_id=refix_run)
        else:
            retried, _, failures = invoke_graphs([refix], specs=ctx.specs, runner=ctx.runner, run_id=refix_run, max_parallel=1)
        why = failures[0] if failures else None
        if why is None and retried[0].get("failed_node") is not None:
            why = f"node '{retried[0]['failed_node']}' failed"
        if why is None:
            why = unapproved(retried[0])
        if why is not None:
            build["evidence"].append({"check": "fix loop re-entry", "output": f"not approved: {why}"})
            if retried and is_mapping(retried[0]):
                # Not saved as the task's result, but its plan, build and reviews ran: they are this run's steps.
                record_steps(ctx, retried[0], phase=phase, task=task)
            break
        result = retried[0]
        result.setdefault("initiative", ctx.initiative_id)
        result.setdefault("phase", phase)
        result.update(body_stamp(item))
        save_task_result(ctx, result, phase=phase, task=task)
        spent += _as_float(as_mapping(result.get("fix_loop")).get("attempts") or 1)
        routes.append("revise")
        names = ", ".join(str(c["name"]) for c in build["checks"] if not c.get("passed"))
        _release_task(ctx, phase, task)
        drop_lane(ctx, phase, task)
        git_in("-C", str(ctx.repo), "branch", "-D", ctx.scratch_branch(task))
        build = build_task(ctx, phase=phase, task=task, result=result, verify=verify_of(by_id, task))
        build["evidence"].append(
            {"check": "fix loop re-entry", "output": f"revise after {names}: attempts spent {spent:g} of {limit + 1}"}
        )
    return result, ({**build, "refix": routes} if routes else build)


def unapproved(result: Mapping[str, object]) -> str | None:
    """Why the fix loop refused this task, or None when it approved it.

    Read from the loop's own record, not re-derived. `lifecycle-propose` emits a
    `draft_pr_create` proposal when and only when its reviewers approved the
    build; a result carrying none is a change the loop already decided against,
    and `fix_loop.stopped` says on what grounds.

    The epic used to apply that patch anyway: it opened a worktree, ran the
    checks, and then paid a chunk validator and a phase validator to look at a
    task whose `review_verdict` still read `revise`. The validator refused it —
    correctly, on evidence that said the reviewers wanted changes — but it
    refused with its own reasoning rather than the loop's, so the record named
    a validator gap where the real answer was "the fix build changed nothing".
    A verdict a later build was supposed to supersede, and did not, must never
    reach a validator as though it were current.
    """
    if any(
        is_mapping(item) and item.get("kind") == "draft_pr_create"
        for item in as_list(result.get("proposals"))
    ):
        return None
    loop = as_mapping(result.get("fix_loop"))
    stopped = str(loop.get("stopped") or "") or "the reviewers did not approve the build"
    attempts = loop.get("attempts")
    counted = f" after {attempts} build attempt{'s' if attempts != 1 else ''}" if attempts else ""
    arbitration = as_mapping(result.get("arbitration"))
    if arbitration.get("verdict"):
        verdict = str(arbitration["verdict"])
        reasoning = str(arbitration.get("reasoning") or "").split(". ", 1)[0].split(".", 1)[0]
        detail = f", sided_with {arbitration.get('sided_with')}: {reasoning}"
    else:
        verdict = str(as_mapping(result.get("review")).get("verdict") or "") or "none recorded"
        objections = as_list(as_mapping(result.get("adversary")).get("objections"))
        detail = f": {as_mapping(objections[0]).get('claim') or ''}" if objections else ""
    return (
        f"the fix loop stopped: {stopped}{counted}; the last review verdict was "
        f"'{verdict}'{detail} and no build was approved"
    )


def task_outcome(
    review_verdict: str | None,
    arbitration_verdict: str | None,
    quarantine_reason: str | None,
    landed: bool,
) -> str:
    """One of `landed`, `approved_not_landed`, `rejected`, `harness_fault`."""
    if landed:
        return "landed"
    if quarantine_reason is not None and is_harness_fault(quarantine_reason):
        return "harness_fault"
    if review_verdict == "approve" or arbitration_verdict == "approve":
        return "approved_not_landed"
    return "rejected"


# ── the driver ──────────────────────────────────────────────────────────────

WORK_STATES = ("files", "store")
POSTGRES_URL_PREFIXES = ("postgresql://", "postgresql+psycopg://", "postgres://")


def checked_work_state(value: object) -> str:
    """`value` when it is one of WORK_STATES; anything else is refused, never read as the default."""
    if value not in WORK_STATES:
        raise ValueError(f"provider profile 'work_state' must be one of {', '.join(WORK_STATES)}, not {value!r}")
    return str(value)


def work_state_of(profile: Mapping[str, object]) -> str:
    """The profile's `work_state`: which side owns a task's state. An absent key means store when `storage_url` is Postgres, else files."""
    if "work_state" in profile:
        return checked_work_state(profile["work_state"])
    if str(profile.get("storage_url") or "").startswith(POSTGRES_URL_PREFIXES):
        return "store"
    return "files"


def body_stamp(item: Mapping[str, object] | None) -> dict[str, str]:
    """`{"body_sha": ...}` of the work item's own body, the ticket the build was on; {} for an item with no body.

    The item's text, not the prompt's: a carried-forward history is not the ticket. Under work_state store the record is
    the attempt (`core.run_record`), so the chair compares this with the row's current body, as it does a file attempt's."""
    body = (item or {}).get("body")
    return {"body_sha": workstore.body_sha(body)} if isinstance(body, str) else {}


def collected_test_ids(worktree: Path) -> set[str] | None:
    """`pytest --collect-only -q` in `worktree`, read back as node ids.

    `None` means unmeasurable: pytest could not launch (mirrors `run_checks`'s
    own `FileNotFoundError`/`OSError` handling, a harness fault rather than a
    reason to end the run), or it collected zero ids (a non-pytest repo, a
    collection error, a test-less tree), or collection ran past 300 seconds.
    An empty set is not a passing floor,
    `set() <= anything` is true and would let any edit through untested.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q"],
            cwd=worktree, capture_output=True, text=True, timeout=300,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    return collected_ids(proc.stdout) or None
