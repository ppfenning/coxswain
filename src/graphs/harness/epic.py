"""The epic-swarm driver: a whole initiative, phase by phase, landing nothing.

NOT a graph, and it must never acquire a `SPEC`. It walks a phase graph, blocks
on fan-outs, creates branches, runs checks and merges into a stack — a graph is
`run(args, runner) -> dict`, pure and replayable, and this is none of those. It
lives beside `phase.py` and `invoke.py`, which is where the contract already put
everything that owns a side effect.

What it does, per phase, in this order and no other:

    branch from the parent phase's head -> fan out `lifecycle` per ready task
    -> apply, check and commit each patch in a worktree the harness owns
    -> escalate anything whose diff touched governance
    -> VALIDATE (`phase-validate`, invoked like any other graph)
    -> one gate for the phase -> execute what was cleared -> record the phase

Validation sits between the fan-out and the merges on purpose. The phase verdict
judges the union of what the tasks produced, before any of it is joined to the
phase branch, so a phase that does not add up is caught while nothing has moved.
The alternative — merge first, re-read the branch afterwards — asks the verdict
to be about a state the driver has already committed to.

**Nothing here merges to a default branch.** There is no code path that emits or
executes `merge_main`; the swarm's terminal state is branches and proposals. See
the comment where the merges execute.

Two things are load-bearing about the record. Every phase records its own
manifest under `f"{run_id}:{phase}"` — one cartridge, one scope, so
`_require_single_scope` stays satisfiable — and an auto-cleared proposal gets no
gate diff and no ledger row, because autonomy is spent by acting and re-earned
only at a gate.

Checks are the cartridge's `landing_areas.checks` plus whatever the repository
itself declares in a root `.agent-checks` file, read once at the edge and
merged in by `_Ctx.checks`.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
import types
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Any, Literal
from urllib.parse import urlencode

import yaml

from core import ledger, workstore
from core.cartridge import integration_checks_setting, test_database_setting
from core.landing import (
    DEFAULT_LAND_AT,
    land_gate_level,
    need_met,
    pr_branch_name,
    resolve_land_at,
)
from core.manifest import append_ledger, build_manifest, gate_diff
from core.phase_hold import phase_holds
from core.workstore import WorkStoreError, record_attempt
from graphs._contract import proposal
from graphs.delivery import rescue_review
from graphs.delivery.budget_estimate import estimate_budget
from graphs.delivery.lifecycle_propose import DEFAULT_FIX_ATTEMPTS
from harness import pool_edge, rescue_checks, rescue_select, termination, warm_pool, work_mirror
from harness.assembly import assemble
from harness.autonomy import split_by_policy
from harness.base_check import Expected, expected_merges, gaps, stale_base_stop
from harness.base_sync import fetch_origin, merge_in
from harness.base_sync import holds as base_holds
from harness.cause_model import cause_evidence, classify_with_model, one_line
from harness.cause_rule import _TRANSIENT_AUTH, classify_cause, is_auth_failure
from harness.check_deps import load_dep_map
from harness.check_gate import gate_checks, gate_commands, repo_gate_lock
from harness.check_scope import _is_test_file, pytest_component, render_selection, select_checks
from harness.checks import (
    HARNESS_FAULT_PREFIX,
    _tail_lines,
    all_passed,
    check_feedback,
    check_timeout,
    checks_evidence,
    collected_ids,
    coverage_floor_holds,
    fixable_checks,
    is_harness_fault,
    outside_surface_files,
    quarantine_reason,
    refix_route,
    repo_checks,
    run_checks,
)
from harness.cos import pid_alive
from harness.courier_adapter import publish_run_exited
from harness.courier_adapter import send as send_to_courier
from harness.digest import build_digest
from harness.escalate import escalate_self_modification, touched_paths
from harness.gate import apply_arm_for, auto_apply, gate
from harness.handoff import build_handoff, plan_context
from harness.integration_scope import scope_integration
from harness.invoke import Invocation, invoke_graphs
from harness.lane_database import database_name, lane_env
from harness.lane_database_create import create_lane_database, creation_failure
from harness.lane_database_drop import drop_lane_database
from harness.lane_workers import lane_workers_from, workers_env
from harness.lanes import lane_bound
from harness.phase_remote import FetchOutcome, Kind, fetch_branch, materialise_local, missing_branch_cause, push_branch
from harness.resume import load_result, reusable, save_result
from harness.scheduler import plan_launches
from harness.stack_refresh import refresh_branch, refresh_reason, refresh_record
from harness.stale_base import is_patch_empty, missing_paths, stale_base_reason, ticket_paths
from harness.store_dialect import Connection, connect
from harness.store_lease import assert_epoch, renew
from harness.store_pause import clear_paused, is_paused
from harness.store_retry import retry_store_write
from harness.store_write import Store, record_handoff, record_merge, upsert_work_item
from harness.surface_heal import heal_bare_filenames, heal_new_markers, malformed_surfaces, notes_out_of_surfaces
from harness.ticket_check import check_ticket, ticket_lookups
from harness.waves import compute_waves, parent_lines, phase_parents, unreached_phases, widest_wave
from harness.worktree import (
    apply_patch,
    create_worktree,
    keep_worktree,
    link_venv,
    prune_registrations,
    read_lock,
    registry_lock,
    release_stale_holders,
    remove_worktree,
)
from runner.claude_code_runner import files_touched_from_patch
from runner.protocol import LimitStop, RunnerError

__all__ = ["PoolSetting", "branch_action", "phase_order", "phase_parents", "rescue_task", "run_epic"]

_log = logging.getLogger(__name__)

LIFECYCLE = "lifecycle"
VALIDATE = "validate"
# `graphs/ops/triage_quarantine.py`'s `GRAPH_NAME` is "triage" (docs/design/triage.md
# §1), but its `SPEC.name` — the registry key `invoke_graphs` looks up — is
# "triage-quarantine", because "triage" already names alert triage's subcommand.
TRIAGE = "triage-quarantine"

# The principal names the driver AND the graph whose work it records, so a
# ledger row from a swarm stays distinguishable from the same graph run alone.
PRINCIPAL = "epic-swarm(lifecycle-propose)"

# Commits the driver makes are mechanical — it saves an applied patch and joins
# branches, it never authors. The identity is passed with -c so a test, or a
# machine with no global git config, needs no setup to run this.
_IDENTITY = ("-c", "user.email=epic-swarm@invalid", "-c", "user.name=epic-swarm")

def opens_pr(land_at: str, grain: str) -> bool:
    """A review PR is opened for a `task` or a `level` only when it is the configured landing granularity."""
    return land_at == grain


_DRAFT_KINDS = frozenset({"draft_pr_create", "self_modification"})

# How much of a task's patch the validators see. All of it for any real task;
# the bound only stops a pathological diff from swamping the phase prompt.
PATCH_FOR_VALIDATION_CHARS = 120_000

# Two recorded attempts and a third run is refused rather than tried again.
# A refusal past this point is not itself an attempt, so it must not grow the
# count it is enforcing — see `_run_phase`, which quarantines these tasks with
# a plain `quarantined.append` and `_quarantine_row` rather than `_quarantine_task`.
ATTEMPT_CAP = 2

# Distinct from a clean run's implicit 0, for a caller to act on the pause.
EXIT_PAUSED = 3

# A phase whose unlanded commits are all approved tasks with a land in flight: not a failure, not a pause.
EXIT_AWAITING_LAND = 4

# docs/design/landing-model.md §5: the existing style_pass brief, plus the one
# sentence that scopes it to a whole phase rather than one task's diff.
_STYLE_PASS_PROMPT = (
    "Make the narrow style edit an approved diff still needs, or return nothing. "
    "Remove duplication the tickets introduced separately; remove shims whose exit "
    "condition the phase has met."
)

_CHECK_FIX_PROMPT = (
    "The diff below was approved, then a configured check failed on it. Make the smallest "
    "edit that fixes the failure shown, as a unified diff against the tree with the "
    "approved diff already applied, or return nothing."
)

_STYLE_PASS_SCHEMA = {
    "type": "object",
    "properties": {"patch": {"type": "string"}},
    "required": ["patch"],
    "additionalProperties": False,
}


def _carry_forward(body: str, attempts: list[dict[str, Any]], patch: str | None, *, limit: int) -> str:
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


def _permit_outside_surfaces(body: str, surfaces: Sequence[str], reason: str) -> tuple[str, list[str]]:
    """Body and surfaces with the files named by an outside-surfaces `reason` added, once each.

    Original surfaces keep their order and come first. Any other reason returns both unchanged.
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


def _git(*args: str, cwd: Path | None = None) -> tuple[bool, str]:
    """Run one git command and report what happened, never what was intended."""
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False, f"git {' '.join(args)} timed out after 120s"
    return proc.returncode == 0, (proc.stderr or proc.stdout).strip()


def _push_review_branch(ctx: _Ctx, task: str) -> tuple[bool, str]:
    """Push the task's scratch branch to origin as `review/<initiative>/<task>`."""
    return _push_review_ref(ctx, ctx.scratch_branch(task), task)


def _push_review_ref(ctx: _Ctx, local: str, name: str) -> tuple[bool, str]:
    """Push the local branch `local` to origin as `review/<initiative>/<name>`."""
    return _git("-C", str(ctx.repo), "push", "origin", f"{local}:refs/heads/review/{ctx.initiative_id}/{name}")


def _publish_branch(ctx: _Ctx, branch: str) -> str | None:
    """Push `branch` to origin. None on success or with no origin, else a warning; a failed push never stops the build."""
    ok, detail = push_branch(ctx.repo, branch)
    return None if ok else f"push of {branch} failed: {detail}"


def _fetch_phase_branches(ctx: _Ctx, branches: Sequence[str]) -> dict[str, FetchOutcome]:
    """Make each of `branches` a plain local branch where origin holds it; a branch already local is left alone."""
    outcomes: dict[str, FetchOutcome] = {}
    for branch in branches:
        outcome = fetch_branch(ctx.repo, branch)
        if outcome.kind == Kind.FETCHED:
            ok, detail = materialise_local(ctx.repo, branch)
            outcome = outcome if ok else FetchOutcome(Kind.FETCH_FAILED, detail)
        outcomes[branch] = outcome
    return outcomes


def _await_launched_branch(ctx: _Ctx, branch: str, outcome: FetchOutcome, *, patience: float = 60.0) -> FetchOutcome:
    """A branch missing everywhere while its phase is launched and unfinished is still being cut: wait for the cut, not the phase."""
    owner = ctx.launched.get(branch.removeprefix(f"epic/{ctx.initiative_id}/"))
    if outcome.kind != Kind.MISSING_EVERYWHERE or owner is None:
        return outcome
    deadline = time.monotonic() + patience
    while not owner.done() and not _branch_exists(ctx, branch) and time.monotonic() < deadline:
        time.sleep(0.05)
    return fetch_branch(ctx.repo, branch)


def _fetch_or_stop(
    ctx: _Ctx,
    record: dict[str, Any],
    fetch: Sequence[str],
    required: Sequence[str],
    loose: Sequence[str] = (),
    parent_branches: Sequence[str] = (),
) -> dict[str, Any] | None:
    """Fetch `fetch`; the stopped record when a branch the phase needs is missing everywhere or origin could not be asked.

    The needed branches are `required`, plus the parents' when a `loose` ref (a commit id) is not in this repo,
    since that commit can only come from a parent's branch. A parent nothing is needed from is fetched, never demanded.
    """
    outcomes = _fetch_phase_branches(ctx, fetch)
    unresolved = any(not _git("-C", str(ctx.repo), "rev-parse", "--verify", "--quiet", f"{r}^{{commit}}")[0] for r in loose)
    demanded = list(dict.fromkeys([*required, *(parent_branches if unresolved else [])]))
    outcomes = {b: _await_launched_branch(ctx, b, o) if b in demanded else o for b, o in outcomes.items()}
    for branch in demanded:
        if outcomes[branch].kind in (Kind.MISSING_EVERYWHERE, Kind.FETCH_FAILED):
            return _missing_branch_phase(record, branch, missing_branch_cause(branch, outcomes[branch]))
    return None


def _phase_branches_to_fetch(
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


def _note_push_failure(record: dict[str, Any], warning: str | None) -> None:
    """Add `warning` to the phase record's `remote_push_failed`, a single string joined with '; '."""
    if warning:
        record["remote_push_failed"] = "; ".join(filter(None, [record.get("remote_push_failed"), warning]))


def _open_review_pr(ctx: _Ctx, *, title: str, body: str, head: str, base: str) -> tuple[bool, str]:
    """Open a PR for `head` against `base`; the detail is its URL on success."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "create", "--title", title, "--body", body, "--head", head, "--base", base],
            cwd=ctx.repo, capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return False, "gh pr create timed out after 120s"
    return proc.returncode == 0, (proc.stdout if proc.returncode == 0 else proc.stderr or proc.stdout).strip()


def _existing_pr(ctx: _Ctx, head: str) -> tuple[bool, str]:
    """(known, url): known is False when the forge could not be asked, so "no PR" is never guessed from an error."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", head, "--json", "url", "--jq", ".url"],
            cwd=ctx.repo, capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode == 0:
        return True, proc.stdout.strip()
    if "no pull requests found" in proc.stderr.lower():
        return True, ""
    return False, (proc.stderr or proc.stdout).strip()


def _open_grain_pr(
    ctx: _Ctx, *, grain: str, name: str, local: str, title: str, base: str
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
    ok, detail = _open_review_pr(
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
    head_ok, head_out = _git(*git, "rev-parse", "--abbrev-ref", "HEAD")
    origin_ok, origin_out = _git(*git, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    local_ok, local_out = _git(*git, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    remotes_ok, remotes = _git(*git, "remote")
    has_origin = remotes_ok and "origin" in remotes.split()
    if has_origin:
        fetched, detail = _git(*git, "fetch", "origin")
        if not fetched:
            raise RuntimeError(f"git fetch origin failed in {repo}: {detail[:200]}")
    tracked_ok, tracked_out = _git(*git, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin")
    known = (set(local_out.split()) if local_ok else set()) | (
        {name.removeprefix("origin/") for name in tracked_out.split()} if tracked_ok else set()
    )
    ref = origin_ref(
        base,
        origin_out.strip() if origin_ok else None,
        known,
        head_out.strip() if head_ok else "HEAD",
    )
    return ref if has_origin else ref.removeprefix("origin/")


# A mid-run mirror or a pause clear waits this long for a lost connection; the run's exit record gets the full five minutes.
_END_WRITE_WINDOW_S = 60.0
_EXIT_WINDOW_S = 300.0


def _reopen_url(conn: Connection) -> str | None:
    """A URL `connect` accepts for the database `conn` is on, or None when it cannot be reopened (an in-memory sqlite)."""
    raw = conn.raw
    if isinstance(raw, sqlite3.Connection):
        file = next((row[2] for row in raw.execute("PRAGMA database_list") if row[1] == "main"), "")
        return f"sqlite:///{file}" if file else None
    # psycopg's `info.get_parameters()` never returns the password; the libpq `pgconn.info` does.
    params = {item.keyword.decode(): item.val.decode() for item in raw.pgconn.info if item.val}
    return "postgresql:///?" + urlencode(params)


def reopen_connection(conn: Connection) -> None:
    """Replace the driver connection under `conn` with a fresh one to the same database.

    Every holder of `conn` (the heartbeat, the runner) sees the new connection. The old one is closed only
    after the new one is open, so a server still down leaves `conn` as it was and raises for the retry to repeat.
    """
    url = _reopen_url(conn)
    if url is None:
        return
    fresh = connect(url)
    old, conn.raw = conn.raw, fresh.raw
    with contextlib.suppress(Exception):  # the old connection is already dead
        old.close()


@dataclass(frozen=True)
class _StoreRetry:
    """How a store write is retried across a lost connection. The default reconnects nothing: see `for_store`."""

    reconnect: Callable[[], None] = lambda: None
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    @classmethod
    def for_store(
        cls, store: Store, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep
    ) -> _StoreRetry:
        """Retries that reconnect `store`'s own connection."""
        return cls(reconnect=lambda: reopen_connection(store.conn), clock=clock, sleep=sleep)

    def write(self, call: Callable[[], Any], window_s: float) -> Any:
        """Run `call`, reconnecting and retrying a lost connection for `window_s`; any other error, or the window's end, raises."""
        return retry_store_write(
            call, reconnect=self.reconnect, clock=self.clock, sleep=self.sleep, window_s=window_s
        )


@dataclass(frozen=True)
class PoolSetting:
    """Warm pool of task worktrees on this host; `run_epic` leaves it off unless one is passed.

    `keep` is the most idle directories left after the run. The pool's capacity is `pool_size(max_parallel)`.
    The clock is wall time because the pool index outlives the process.
    """

    pool_root: Path
    sync_argv: Sequence[str]
    lock_names: Sequence[str]
    keep: int
    clock: Callable[[], float] = time.time


@dataclass(frozen=True)
class _Ctx:
    """Everything the per-phase work needs, fixed for the whole run.

    A frozen record rather than a dozen parameters threaded through six
    functions: the phase loop is where the interesting decisions are, and they
    read better when the configuration is not standing in front of them.
    """

    repo: Path
    cartridge: Mapping[str, Any]
    runner: Any
    specs: Mapping[str, Any]
    run_id: str
    date: str
    max_parallel: int
    ledger_path: Path
    provider_profile: str
    runs_dir: Path
    worktree_root: Path
    assume: str | None
    fix_attempts: int | None
    initiative_id: str
    default_ref: str
    land_at: str = "ticket"
    resume_from: str | None = None
    repo_checks: list = field(default_factory=list)
    store: Store | None = None
    epoch: int | None = None
    lease_name: str | None = None
    work_state: str = "files"
    retry: _StoreRetry = field(default_factory=_StoreRetry)
    # `landing_areas.test_database`, read once: each task lane runs its checks against its own database.
    test_database: Mapping[str, str] | None = None
    # Workers each lane's checks may use; None when no lane capacity was stated.
    lane_workers: int | None = None
    # Run-level and mutable, shared by `replace`: lanes holding a database, and the drop records (the cleanup list).
    lane_names: dict[tuple[str, str], str] = field(default_factory=dict, compare=False, repr=False)
    lane_drops: list[dict[str, Any]] = field(default_factory=list, compare=False, repr=False)
    pool: PoolSetting | None = None
    # Run-level and mutable: the lease each pooled task build holds, with the lock digest read when it was acquired.
    pool_leases: dict[tuple[str, str], tuple[pool_edge.Lease, str]] = field(
        default_factory=dict, compare=False, repr=False
    )
    # The one mutable member of a frozen record: file listings by commit (see `_default_branch_paths`).
    surface_paths: dict[str, frozenset[str]] = field(default_factory=dict, compare=False, repr=False)
    # Guards writes to the shared `items` list and the lazy fill of `surface_paths`; `replace` shares it across per-phase copies.
    items_lock: threading.Lock = field(default_factory=threading.Lock, compare=False, repr=False)
    # Run-level and mutable: the future of each phase this run launched, so a dependent that starts early can wait for the cut.
    # The scheduler thread sets one key per launch and workers only `get`, each atomic, and a parent is set before any dependent starts.
    launched: dict[str, Future[Any]] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        """Refuse a bad per-check timeout at build, before any model call; the ValueError names the check."""
        for check in self.checks:
            check_timeout(check, 600)

    # ── names, in one place, so the topology is readable ─────────────────────
    def phase_branch(self, phase: str) -> str:
        return f"epic/{self.initiative_id}/{phase}"

    def pr_branch(self) -> str:
        return pr_branch_name(self.initiative_id)

    def pr_branch_ref(self) -> str | None:
        """The pr branch as a ref git can cut from: the local branch, else its copy on origin, else None."""
        name = self.pr_branch()
        for ref in (f"refs/heads/{name}", f"refs/remotes/origin/{name}"):
            if _git("-C", str(self.repo), "rev-parse", "--verify", "--quiet", ref)[0]:
                return ref.removeprefix("refs/heads/").removeprefix("refs/remotes/")
        return None

    def draft_branch(self, phase: str, task: str) -> str:
        # NOT `epic/<initiative>/<phase>/<task>`: git cannot hold both
        # `refs/heads/epic/i/p1` and `refs/heads/epic/i/p1/t1`, because one is a
        # file where the other needs a directory. The draft namespace is
        # therefore flattened with `--` under the phase, which keeps
        # `git branch --list 'epic/<initiative>/*'` reading as the stack it is.
        return f"epic/{self.initiative_id}/{phase}--{task}"

    def scratch_branch(self, task: str) -> str:
        # Harness-owned, per run, never promoted: this is the namespace the
        # contract's worktree exception covers. The draft branch above is
        # created only after the gate.
        return f"agents/{self.run_id}/{task}"

    def phase_worktree(self, phase: str) -> Path:
        return self.worktree_root / self.run_id / phase

    def task_worktree(self, phase: str, task: str) -> Path:
        held = self.pool_leases.get((phase, task))
        return held[0].path if held else self.phase_worktree(phase) / task

    @property
    def checks(self) -> list[Mapping[str, Any]]:
        cartridge_checks = list((self.cartridge.get("landing_areas") or {}).get("checks") or [])
        known = {c.get("cmd") for c in cartridge_checks}
        extra = [c for c in self.repo_checks if c.get("cmd") not in known]
        return cartridge_checks + extra

    @property
    def level_checks(self) -> list[Mapping[str, Any]]:
        """The cartridge's level gate; never joined with `.agent-checks`, and never read by a task build."""
        return list(gate_checks(self.cartridge.get("landing_areas") or {}, "level"))

    @property
    def integration_checks(self) -> list[Any]:
        """The cartridge's touched-package integration checks; empty when none are configured."""
        return integration_checks_setting(self.cartridge)

    @property
    def bound(self) -> Mapping[str, Any]:
        return self.cartridge.get("skills") or {}


# ── the phase graph ─────────────────────────────────────────────────────────


def phase_order(parents: Mapping[str, set[str]]) -> tuple[list[str], list[str]]:
    """Phases in dependency order with ties broken by name, then the unorderable.

    Ties break by name so two runs over one initiative walk the phases in the
    same order — the same reason `invoke_graphs` reads its results back sorted.
    Whatever is left over sits in a cycle BETWEEN phases: a task DAG can be
    acyclic while the phase graph it induces is not, and a cycle is reported
    rather than resolved.
    """
    remaining = {phase: set(deps) for phase, deps in parents.items()}
    ordered: list[str] = []
    while remaining:
        free = sorted(phase for phase, deps in remaining.items() if not deps - set(ordered))
        if not free:
            break
        for phase in free:
            ordered.append(phase)
            del remaining[phase]
    return ordered, sorted(remaining)


def declared_phases(initiative: Mapping[str, Any], items: Sequence[Mapping[str, Any]]) -> list[str]:
    """Phase ids in declared order; the sorted phase names on the items when no entry carries an id."""
    declared = [str(p["id"]) for p in initiative.get("phases") or [] if isinstance(p, Mapping) and p.get("id")]
    return declared or sorted({str(i["phase"]) for i in items if i.get("phase")})


def unreached_warning(unreached: Mapping[str, Mapping[str, Sequence[str]]]) -> str | None:
    """One warning line naming the phases that build without earlier phases they share files with, else None."""
    if not unreached:
        return None
    return (
        f"  warning: phase(s) {', '.join(sorted(unreached))} will build without the earlier phases listed as not "
        "inherited; a task dependency on a task in that phase would add it"
    )


_EVIDENCE_LINE_CAP = 400


def _read_evidence_file(worktree: str, rel: str) -> str | None:
    """The reader `phase-validate` calls for a chunk verdict's `needs_evidence`.

    The one place this driver opens a file on the graph's behalf, injected in
    as an argument per docs/GRAPH-CONTRACT.md clause 4 — the graph itself
    never touches a filesystem. The file-count cap and the check that keeps a
    request inside `worktree` are the graph's own, over the names alone; this
    only caps line count and reports an unreadable path as `None`, never a
    raise, because one bad name must not cost the rest of the phase's verdict.
    """
    try:
        text = Path(worktree, rel).read_text()
    except OSError:
        return None
    return "\n".join(text.splitlines()[:_EVIDENCE_LINE_CAP])


def _phase_goal(initiative: Mapping[str, Any], phase: str) -> str:
    """The phase's ORIGINAL goal — the thing `validate_phase` judges against.

    A work store's phases are bare names (`workstore.phases` returns the set of
    directory names), so where a goal was never recorded the honest substitute
    is the phase's name plus the initiative's own prose. Not a restatement of
    the task list: that is exactly what the phase verdict must never be allowed
    to reduce to. Where a decompose-produced initiative carries `{id, goal}`
    phase entries, they win.
    """
    for entry in initiative.get("phases") or []:
        if isinstance(entry, Mapping) and str(entry.get("id")) == phase and entry.get("goal"):
            return str(entry["goal"])
    body = str(initiative.get("body") or "").strip()
    return f"phase '{phase}' of initiative '{initiative.get('id')}'" + (f"\n\n{body}" if body else "")


# ── branch topology ─────────────────────────────────────────────────────────


def _branch_exists(ctx: _Ctx, branch: str) -> bool:
    ok, _ = _git("-C", str(ctx.repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    return ok


def _branch_ahead(ctx: _Ctx, branch: str) -> bool:
    """True when `branch` holds commits the default ref lacks. A missing branch is not ahead."""
    ok, out = _git("-C", str(ctx.repo), "rev-list", "--count", f"{ctx.default_ref}..refs/heads/{branch}")
    return ok and out.isdigit() and int(out) > 0


class _Opened(tuple):
    """`(ok, detail, reused)`, with the worktree paths cleared on the way in as `.cleared`.

    A tuple subclass so every caller and test that unpacks the triple keeps working.
    """

    cleared: tuple[str, ...]

    def __new__(cls, ok: bool, detail: str, reused: bool, cleared: Sequence[Path] = ()) -> _Opened:
        opened = super().__new__(cls, (ok, detail, reused))
        opened.cleared = tuple(str(path) for path in cleared)
        return opened


def _run_alive(ctx: _Ctx) -> Callable[[str], bool]:
    """A run is alive when it is this one, or its `<run_id>.pid` names a running process."""
    return lambda run_id: run_id == ctx.run_id or pid_alive(ctx.runs_dir / f"{run_id}.pid")


def _registered_on_branch(listing: str, path: Path, branch: str) -> bool:
    """True when `git worktree list --porcelain` text registers `path` (resolved) on `refs/heads/<branch>`."""
    target = path.resolve()
    return any(
        f"branch refs/heads/{branch}" in record.splitlines()
        and any(
            line.startswith("worktree ") and Path(line.removeprefix("worktree ")).resolve() == target
            for line in record.splitlines()
        )
        for record in listing.split("\n\n")
    )


def _open_phase_worktree(
    ctx: _Ctx, phase: str, base_ref: str, parents: tuple[str, ...] = (), refs: Sequence[str] | None = None
) -> _Opened:
    """Get a worktree on the phase branch. Returns (ok, detail, reused), with `.cleared` on the result.

    Re-entrancy is the point: a second driver run over the same initiative
    builds on the branch the first one left, rather than starting a parallel
    one beside it under a different run id.

    Prunes stale `git worktree` registrations first: a crashed prior run can
    leave a phantom entry pointing at a directory that is already gone, and
    that phantom is what blocked `tools-chair-rename-18` on 2026-09-08.

    A dead run can also leave its worktree in place holding the branch, so a
    reused branch first releases the holders whose run is no longer alive.
    A registered worktree of this run on the branch is reused.

    A new branch under any parent is cut from `base_ref` and assembled from `refs`, the commits it needs;
    `refs` defaults to every parent's branch. A failure keeps the assembly cause.
    """
    prune_registrations(ctx.repo)
    branch = ctx.phase_branch(phase)
    worktree = ctx.phase_worktree(phase)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if _branch_exists(ctx, branch):
        cleared = release_stale_holders(ctx.repo, branch, ctx.worktree_root, _run_alive(ctx))
        listed, listing = _git("-C", str(ctx.repo), "worktree", "list", "--porcelain")
        if listed and _registered_on_branch(listing, worktree, branch):
            return _Opened(True, "", True, cleared)
        with registry_lock():
            ok, detail = _git("-C", str(ctx.repo), "worktree", "add", str(worktree), branch)
        return _Opened(ok, detail, True, cleared)
    ok, detail = create_worktree(ctx.repo, worktree, branch=branch, base=base_ref)
    wanted = [ctx.phase_branch(p) for p in parents] if refs is None else list(refs)
    if ok and parents and wanted and wanted != [base_ref]:
        assembled = assemble(ctx.repo, worktree, base_ref, wanted, branch)
        if not assembled.ok:
            # Drop the half-made branch, or the next run reuses it as if it were assembled.
            remove_worktree(ctx.repo, worktree)
            _git("-C", str(ctx.repo), "branch", "-D", branch)
            return _Opened(False, ASSEMBLY_FAILED + assembled.cause, False)
    return _Opened(ok, detail, False)


ASSEMBLY_FAILED = "parent branches could not be assembled: "


def _start_failure(detail: str, verb: str) -> str:
    """The reason for a phase whose worktree did not open; a failed assembly keeps its cause as the reason."""
    if detail.startswith(ASSEMBLY_FAILED):
        return detail
    return f"the phase worktree could not be {verb}: {detail}"


def _required_refs(
    items: Sequence[Mapping[str, Any]],
    phase: str,
    parents: Collection[str],
    branch_of: Callable[[str], str],
    base_ref: str,
    *,
    cut: bool = False,
    from_pr: bool = False,
) -> list[str]:
    """The refs a phase branch is checked against (`cut` false) or cut from (`cut` true).

    A phase with no parents, or one cut from the pr branch (`from_pr`), needs `base_ref` alone. Under a
    parent, a need that is unbuilt, done or dropped contributes nothing, and a parent no task needs anything
    from contributes its branch. A need that is approved and merged contributes its stored `merged_commit`
    (the parent's branch when none is stored), so the staleness check judges a branch on what its tasks need.
    With `cut`, such a need contributes the parent's current branch tip instead, so an assembly never starts
    from an older head whose conflict a later commit resolved, and a parent whose tasks are all settled
    contributes its whole branch.
    """
    if not parents or from_pr:
        return [base_ref]
    by_id = {str(i["id"]): i for i in items}
    open_phases = {str(i.get("phase")) for i in items if i.get("state") in ("todo", "ready", "in_progress", "blocked")}
    needed = [
        by_id[str(n)] for i in items if i.get("phase") == phase for n in i.get("needs") or [] if str(n) in by_id
    ]
    refs: set[str] = set()
    for parent in parents:
        edges = [n for n in needed if n.get("phase") == parent]
        if not edges or (cut and parent not in open_phases):
            refs.add(branch_of(parent))
            continue
        refs.update(
            branch_of(parent) if cut else str(n.get("merged_commit") or branch_of(parent))
            for n in edges
            if n.get("state") == "approved" and n.get("merged")
        )
    return sorted(refs)


def _needs_other_phase(items: Sequence[Mapping[str, Any]], phase: str) -> bool:
    """Does a task of `phase` need a task of another phase, whatever that task's state?"""
    phase_of = {str(i["id"]): i.get("phase") for i in items}
    return any(
        phase_of.get(str(n)) not in (None, phase)
        for i in items
        if i.get("phase") == phase
        for n in i.get("needs") or []
    )


def _needs_missing(ctx: _Ctx, phase: str, refs: Sequence[str]) -> bool:
    """Is any commit the phase branch needs absent from it?

    `merge-base --is-ancestor` is the whole question. A parent that merges unrelated work moves its
    head and changes nothing here, since only the commits the branch needs are asked about.
    """
    return any(
        not _git("-C", str(ctx.repo), "merge-base", "--is-ancestor", ref, ctx.phase_branch(phase))[0] for ref in refs
    )


def branch_action(reused: bool, head_moved: bool, has_own_commits: bool) -> str:
    """Block only a stale branch that has something of its own to lose; recreate the rest."""
    if not (reused and head_moved):
        return "proceed"
    return "block" if has_own_commits else "recreate"


def _task_of(subject: str) -> str | None:
    head, sep, task = subject.partition(": ")
    return task if sep and head.startswith("epic ") else None


def unlanded(cherry_lines: Sequence[str], done_tasks: set[str]) -> list[str]:
    """Subjects of `git cherry -v` `+` lines that are neither a done task's commit nor one of our merges.

    A landing can rewrite a done task's patch, so its patch-id no longer matches main's.
    A task literally named `merge ...` reads as a merge commit.
    """
    subjects = [line.split(" ", 2)[2] if line.count(" ") >= 2 else "" for line in cherry_lines if line.startswith("+ ")]
    return [s for s in subjects if (t := _task_of(s)) is None or not (t in done_tasks or t.startswith("merge "))]


def classify_unlanded(cherry_lines: Sequence[str], task_states: Mapping[str, str]) -> str:
    """What the phase branch's `+` commits mean, by their tasks' work-store states.

    `landed`: none left, or every one is a done task's (already squashed onto main).
    `awaiting_land`: every one is an approved task whose land is still in flight.
    `rebase`: a mix of done and approved. `rebuild`: every one is a ready task's (sent back with
    `route edit --rebuild`, so it builds again), beside any done ones. `block`: any other commit or task state.
    """
    tasks = [_task_of(s) for s in unlanded(cherry_lines, set())]
    states = {task_states.get(t, "") if t is not None else "" for t in tasks}
    if not states or states == {"done"}:
        return "landed"
    if states == {"approved"}:
        return "awaiting_land"
    if states == {"approved", "done"}:
        return "rebase"
    return "rebuild" if states <= {"ready", "done"} else "block"


def blocking_commits(cherry_lines: Sequence[str], task_states: Mapping[str, str]) -> list[str]:
    """One `task <id> state=<state>` per task whose unlanded commit is on the branch, in commit order, or `commit <subject>` when no task owns it."""
    subjects = unlanded(cherry_lines, set())
    found = [f"task {t} state={task_states.get(t) or 'unknown'}" if (t := _task_of(s)) is not None else f"commit {s!r}" for s in subjects]
    return list(dict.fromkeys(found))


def waiting_tasks(cherry_lines: Sequence[str]) -> list[str]:
    """Ids of the tasks owning an unlanded commit, once each, in commit order."""
    ids = (_task_of(s) for s in unlanded(cherry_lines, set()))
    return list(dict.fromkeys(t for t in ids if t is not None))


def awaiting_land_lines(record: Mapping[str, Any], run_id: str, repo: Path | str) -> list[str]:
    """The phase line, then one `cox runs land` per waiting task, for an awaiting_land record whose land is due; else nothing.

    The flags mirror the chair's land action (`chair_exec`); this layer does not import it.
    """
    if record.get("status") != "awaiting_land":
        return []
    tasks = list(record.get("awaiting_tasks") or [])
    head = f"phase {record.get('phase')}: awaiting land of {len(tasks)} approved task(s): {', '.join(tasks)}"
    return [head, *(f"cox runs land {run_id} --task {t} --repo {repo} --apply" for t in tasks)]


def blocked_line(record: Mapping[str, Any]) -> str | None:
    """`phase <p>: blocked: <reason>` for a blocked or interrupted phase record, else None."""
    status = record.get("status")
    if status not in ("blocked", "interrupted"):
        return None
    return f"phase {record.get('phase')}: {status}: {record.get('reason') or 'no reason was recorded on the phase'}"


def open_in_phase(
    items: Sequence[Mapping[str, Any]], phase: str, in_run: Collection[str], land_at: str
) -> list[str]:
    """Ids of `phase` items outside this run whose stored state does not meet `need_met` for `land_at`, in item order."""
    return [
        str(item["id"])
        for item in items
        if item.get("phase") == phase
        and str(item["id"]) not in in_run
        and not need_met(land_at, str(item.get("state")), bool(item.get("merged")))
    ]


def outside_parents_complete(
    items: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]] | None,
    parents: Mapping[str, set[str]],
    named: Collection[str],
    land_at: str,
) -> set[str]:
    """Parents of the `named` phases that are not named themselves and have every task met under `land_at`.

    A stored row's state and merged flag win over the item's. A parent with no tasks is not complete.
    """
    by_task = {str(r["task_id"]): r for r in rows or ()}
    view = [
        {**item, "state": row["state"], "merged": row.get("merged")} if (row := by_task.get(str(item["id"]))) else item
        for item in items
    ]
    outside = {p for phase in named for p in parents.get(phase, ())} - set(named)
    return {p for p in outside if any(i.get("phase") == p for i in view) and not open_in_phase(view, p, (), land_at)}


def _phase_branch_cherry(ctx: _Ctx, phase: str, upstream: str | None = None) -> list[str]:
    """`git cherry -v` of the phase branch against `upstream` (main by default), patch-id semantics; empty if git fails."""
    ok, out = _git("-C", str(ctx.repo), "cherry", "-v", upstream or ctx.default_ref, ctx.phase_branch(phase))
    return out.splitlines() if ok else []


def _rebase(ctx: _Ctx, phase: str, base_ref: str) -> tuple[bool, str]:
    """Replay the phase branch onto the parent's new head, or leave it untouched.

    A conflict aborts and quarantines the phase with git's own diagnosis. There
    is no path here that resolves one unattended: `stack_rebase` is a write kind
    precisely because rewriting a branch other work is stacked on can silently
    discard a commit, and a driver guessing at a resolution would be doing
    exactly that.
    """
    ok, detail, _ = _rebase_reading_conflicts(ctx, phase, base_ref)
    return ok, detail


def _rebase_reading_conflicts(ctx: _Ctx, phase: str, base_ref: str) -> tuple[bool, str, tuple[str, ...]]:
    """`_rebase`, plus the conflicting paths git listed before the abort cleared them."""
    worktree = ctx.phase_worktree(phase)
    branch = ctx.phase_branch(phase)
    ok, fork = _git("-C", str(worktree), "merge-base", base_ref, branch)
    if not ok:
        return False, f"no merge base between {base_ref} and {branch}: {fork}", ()
    ok, detail = _git(*_IDENTITY, "-C", str(worktree), "rebase", "--onto", base_ref, fork.strip())
    if not ok:
        _, listing = _git("-C", str(worktree), "diff", "--name-only", "-z", "--diff-filter=U")
        _git("-C", str(worktree), "rebase", "--abort")
        return False, detail, tuple(sorted(p for p in listing.split("\0") if p))
    return True, f"rebased {branch} onto {base_ref}", ()


def _branch_has(ctx: _Ctx, ref: str, branch: str) -> bool:
    return _git("-C", str(ctx.repo), "merge-base", "--is-ancestor", ref, branch)[0]


def _branch_head(ctx: _Ctx, branch: str) -> str:
    ok, out = _git("-C", str(ctx.repo), "rev-parse", branch)
    return out if ok else ""


def _refresh_level(
    ctx: _Ctx, record: dict[str, Any], phase: str, parent: str | None, recreate: Callable[[], dict[str, Any] | None]
) -> dict[str, Any] | None:
    """Restack a child onto a current parent, else merge the default ref in; the record to return when it blocks."""
    branch, default = ctx.phase_branch(phase), ctx.default_ref
    if ctx.land_at != "ticket" or not record.get("reused_branch") or _branch_has(ctx, default, branch):
        return None
    before = _branch_head(ctx, branch)
    onto, paths = default, ()
    # Levels can start side by side, so a parent is not always refreshed first; such a child must not wait on it.
    stacked = parent is not None and _branch_has(ctx, default, ctx.phase_branch(parent))
    if not stacked:
        merged = refresh_branch(ctx.phase_worktree(phase), branch, default)
        ok, reason, paths = merged.ok, merged.reason, merged.paths
    else:
        onto = ctx.phase_branch(str(parent))
        _, own = _git("-C", str(ctx.repo), "rev-list", "--count", f"{onto}..{branch}")
        if own == "0":
            early = recreate()
            if early is not None:
                return early
            ok, paths = True, ()
        else:
            ok, _, paths = _rebase_reading_conflicts(ctx, phase, onto)
        reason = refresh_reason(branch, onto, paths)
    if not ok:
        record["status"] = "blocked"
        record["reason"] = reason
        record["quarantined"].append({"id": phase, "phase": phase, "grain": "phase", "reason": reason})
        _git("-C", str(ctx.phase_worktree(phase)), "checkout", "--detach", "-q")
        return record
    after = _branch_head(ctx, branch)
    if after != before:
        record["refreshed"] = refresh_record(branch, onto, before, after)
        print(f"  {branch} brought up to {onto}")
    return None


# ── one task's build, applied and measured in a worktree the harness owns ────


def _trace_evidence(calls: Sequence[Any], task: str, patch: str) -> list[dict[str, Any]]:
    """Evidence rows the harness itself observed, per docs/design/validator-reach.md §1.

    Commands are selected by `task_id` — stamped onto the call ledger by
    `ClaudeCodeRunner.run`'s own `task` kwarg — never by `files_touched`, which two
    tasks (or a fix-loop retry) can share. `ctx.runner.calls` accumulates across every
    task and retry, so the LAST matching `role: "build"` call is the one whose patch
    this record actually applied. One `command` row per `commands_run` entry with
    `source == "trace"`, in trace order — a `self_report` entry is a claim, not an
    observation, and is never folded in.

    A resumed task (`--resume-from`) makes no build call in THIS run, so no ledger
    entry matches at all — `files_touched` then falls back to the reconciled `patch`
    itself, still exactly one row, never empty for a non-empty patch.
    """
    call = next(
        (c for c in reversed(calls) if isinstance(c, Mapping) and c.get("role") == "build" and c.get("task_id") == task),
        {},
    )
    commands = [
        {
            "check": "command",
            "source": "trace",
            "output": f"{entry.get('command', '')}\n{_tail_lines(str(entry.get('output') or ''))}",
        }
        for entry in (call.get("commands_run") or [])
        if isinstance(entry, Mapping) and entry.get("source") == "trace"
    ]
    files = call.get("files_touched") or files_touched_from_patch(patch)
    return [*commands, {"check": "files_touched", "source": "trace", "output": "\n".join(files)}]


def _checks_log(ctx: _Ctx, phase: str, task: str) -> dict[str, Any]:
    """`run_checks` log arguments: `runs/<run>/checks/<phase>/<task>/<n>.log`, so tasks never overwrite each other."""
    return {"log_dir": ctx.runs_dir / ctx.run_id / "checks" / phase / task, "log_prefix": f"checks/{phase}/{task}/"}


def _nul_paths(listing: str) -> list[str]:
    return [p for p in listing.split("\0") if p]


def _lane_checks(
    worktree: Path, base: str, checks: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The checks the task's patch can affect, and the selection; every check, with a reason, on any error."""
    known = [c for c in (pytest_component(str(check.get("cmd", ""))) for check in checks) if c]

    def everything(why: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return [dict(c) for c in checks], {
            "components_run": known,
            "components_skipped": [],
            "narrowed": dict.fromkeys(known, "whole"),
            "added_tests": [],
            "reason": dict.fromkeys(known or ["*"], f"fallback {why}"),
        }

    ok, changed = _git("-C", str(worktree), "diff", "--name-only", "--no-renames", "-z", base, "HEAD")
    added_ok, added = _git(
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
        added_tests = [p for p in _nul_paths(added) if _is_test_file(p)]
        scoped = select_checks(checks, _nul_paths(changed), added_tests, import_map)
    except Exception as exc:  # fail open: whatever broke, the full list still runs
        return everything(f"import map failed: {type(exc).__name__}: {exc}")
    return list(scoped.checks), scoped.selection


def _tests_run(results: Sequence[Mapping[str, Any]]) -> int:
    return sum((r.get("counts") or {}).get(k, 0) for r in results for k in ("passed", "failed", "error"))


def _level_failure_reason(phase: str, results: Sequence[Mapping[str, Any]]) -> str:
    """A line naming the level and its first failing level check; '' when every row passed."""
    failing = next((r for r in results if not r.get("passed")), None)
    return "" if failing is None else f"level {phase} failed level check {failing.get('name')}"


def _run_level_checks(ctx: _Ctx, phase: str, record: dict[str, Any]) -> None:
    """Run the level checks once in the level's worktree, under the repo gate lock, and fold the result into `record`.

    No level checks configured: nothing runs and `record` gains no key. A failure marks the level, never a task.
    """
    checks = ctx.level_checks
    if not checks:
        return
    log = {"log_dir": ctx.runs_dir / ctx.run_id / "checks" / phase / "level", "log_prefix": f"checks/{phase}/level/"}
    with repo_gate_lock(ctx.repo):
        results = run_checks(ctx.phase_worktree(phase), checks, env=_checks_env(ctx, {}), **log)
    record["level_checks"] = results
    if reason := _level_failure_reason(phase, results):
        record["status"], record["reason"] = "partial", reason


def _level_integration_reason(phase: str, creation: Mapping[str, Any], results: Sequence[Mapping[str, Any]]) -> str:
    """A line naming the level and what stopped its integration gate; '' when the database exists and every row passed."""
    if setup := creation_failure(creation):
        return f"level {phase}: {setup}"
    failing = next((r for r in results if not r.get("passed")), None)
    return "" if failing is None else f"level {phase} failed integration check {failing.get('name')}"


def _run_level_integration(ctx: _Ctx, phase: str, base: str, record: dict[str, Any]) -> None:
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
    ok, listing = _git("-C", str(worktree), "diff", "--name-only", "--no-renames", "-z", base, "HEAD")
    if not ok:
        record["status"], record["reason"] = "partial", f"level {phase} integration paths unreadable: {listing}"
        return
    scoped = scope_integration(checks, _nul_paths(listing))
    results: list[dict[str, Any]] = []
    if scoped.checks and not creation_failure(creation):
        log = {
            "log_dir": ctx.runs_dir / ctx.run_id / "checks" / phase / "level-integration",
            "log_prefix": f"checks/{phase}/level-integration/",
        }
        with repo_gate_lock(ctx.repo):
            results = run_checks(worktree, list(scoped.checks), env=_checks_env(ctx, lane), **log)
    record["level_integration"], record["level_integration_scope"] = results, scoped.selection
    if reason := _level_integration_reason(phase, creation, results):
        record["status"], record["reason"] = "partial", reason


def _checks_env(ctx: _Ctx, lane: Mapping[str, str]) -> dict[str, str] | None:
    """The process environment with the worker count and the lane variable over it; None when neither applies."""
    workers = {} if ctx.lane_workers is None else workers_env(os.environ, ctx.lane_workers)
    return {**os.environ, **workers, **lane} if workers or lane else None


def _lane_checks_env(ctx: _Ctx, phase: str, task: str) -> dict[str, str] | None:
    """The child environment for one lane's checks, None when there is neither a worker count nor a lane database."""
    return _checks_env(ctx, lane_env(ctx.test_database, ctx.run_id, phase, task))


def _drop_lane(ctx: _Ctx, phase: str, task: str) -> None:
    """Drop the lane's database once its worktree is gone and list the outcome; a lane with no database does nothing."""
    name = ctx.lane_names.pop((phase, task), None)
    if name is None or ctx.test_database is None:
        return
    ctx.lane_drops.append(
        drop_lane_database(ctx.test_database.get("drop"), name, lane_env(ctx.test_database, ctx.run_id, phase, task))
    )


def _lease_task(ctx: _Ctx, *, phase: str, task: str, branch: str) -> tuple[bool, str, dict[str, object]]:
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


def _release_task(ctx: _Ctx, phase: str, task: str) -> None:
    """Give back the task's pooled lease, or remove its worktree when it holds none."""
    held = ctx.pool_leases.pop((phase, task), None)
    if held is None:
        remove_worktree(ctx.repo, ctx.task_worktree(phase, task))
    else:
        pool_edge.release(*held)


def _settle_pool(ctx: _Ctx, *, keep_worktrees: bool) -> None:
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
            _log.warning("pooled worktree for %s/%s not settled: %s: %s", phase, task, type(exc).__name__, exc)
    try:
        pool_edge.trim(ctx.repo, pool.pool_root, pool.keep, pool.clock)
    except Exception as exc:
        _log.warning("pool trim for %s failed: %s: %s", ctx.run_id, type(exc).__name__, exc)


def _build_task(
    ctx: _Ctx, *, phase: str, task: str, result: Mapping[str, Any], verify: Sequence[str] = ()
) -> dict[str, Any]:
    """Apply one task's patch on a scratch branch off the phase branch, and check it.

    Returns the record whose evidence rows the gate will read. Whether the tests
    pass is not something a review node gets to assert, and a task whose checks
    fail has not met its done criteria — done criteria consume machine evidence,
    not claims — so a failure here quarantines WITH the evidence attached rather
    than proposing a merge and hoping.
    """
    patch = str((result.get("build") or {}).get("patch") or "")
    worktree = ctx.task_worktree(phase, task)
    branch = ctx.scratch_branch(task)
    record: dict[str, Any] = {"id": task, "phase": phase, "branch": branch, "evidence": []}

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
    record["evidence"].extend(_trace_evidence(getattr(ctx.runner, "calls", None) or [], task, patch))

    # Commit BEFORE the checks run, so the branch holds exactly the applied
    # patch and nothing else. Checks execute things — a test run drops
    # __pycache__ and friends into the worktree, and an add -A afterwards would
    # commit those byproducts, which then differ per task and collide as binary
    # conflicts at merge time. Found by running the whole driver against a real
    # repository, which is the only place a bug like this can live.
    ok, detail = _git(*_IDENTITY, "-C", str(worktree), "add", "-A")
    if ok:
        ok, detail = _git(
            *_IDENTITY, "-C", str(worktree), "commit", "--allow-empty", "-q",
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
        _, dirty = _git("-C", str(worktree), "status", "--porcelain")
        if dirty:
            _git(*_IDENTITY, "-C", str(worktree), "add", "-A")
            ok, detail = _git(*_IDENTITY, "-C", str(worktree), "commit", "-q", "--amend", "--no-edit")
            if ok:
                record["lint_fixed"] = True
                build_field = result.get("build")
                _, folded = _git("-C", str(worktree), "diff", ctx.phase_branch(phase), "HEAD")
                if isinstance(build_field, dict) and folded:
                    build_field["patch"] = folded + "\n"

    if ctx.checks:
        scoped, selection = _lane_checks(worktree, ctx.phase_branch(phase), ctx.checks)
        results = run_checks(worktree, scoped, env=env, **_checks_log(ctx, phase, task))
        record["checks"] = results
        record["evidence"].extend(checks_evidence(results))
        tests_run = _tests_run(results)
        record["check_scope"] = {**selection, "tests_run": tests_run}
        record["evidence"].append(
            {"check": "checks:scope", "output": f"{render_selection(selection)}; tests_run={tests_run}"}
        )
        reason = quarantine_reason(results)
        if reason:
            record["quarantine"] = reason

    # The ticket's own verification commands, run here because the builder's
    # sandbox cannot. Their output is evidence for the reviewers; a failing one
    # never quarantines, so its results stay out of `quarantine_reason`.
    if verify:
        ran = run_checks(worktree, [{"name": str(i), "cmd": cmd} for i, cmd in enumerate(verify, 1)], env=env)
        record["verify"] = ran
        record["evidence"].extend(checks_evidence(ran, prefix="verify", always_tail=True))
    return record


def _verify_of(by_id: Mapping[str, Mapping[str, Any]], task: str) -> list[str]:
    return [str(c) for c in (by_id.get(task) or {}).get("verify") or []]


_BUDGET_HISTORY_DAYS = 30


def _budget_history(ctx: _Ctx) -> list[tuple[str, int, float]]:
    """The last 30 days of landed build-task cost history for this run's repo, empty with no store.

    `ctx.date` bounds the window, never the wall clock, so an estimate stays
    reproducible from the run's own recorded inputs rather than from when it
    happened to run. Both `ctx.store` and `ctx.repo` are read with `getattr`:
    `_lifecycle_invocation` is also called, in its own unit test, against a
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


def _lifecycle_invocation(
    ctx: _Ctx, task: Mapping[str, Any], *, body: str, fix_attempts: int | None
) -> Invocation:
    # Every build task resolves a budget and where it came from, never
    # nothing. A ticket's own `budget_usd` wins outright and is checked
    # against the cartridge's `build_budget_usd_max` cap further up in
    # `run_phase` (the `over_budget`/`runnable` split), before this function
    # is ever called, and never through `_quarantine_task` here. An estimate
    # is already clamped to [1.00, 4.00], so it can never reach that cap or
    # the quarantine path either.
    if task.get("budget_usd") is not None:
        build_budget_usd, build_budget_source = task["budget_usd"], "ticket"
    else:
        build_budget_usd, _source = estimate_budget(
            _budget_history(ctx), str(getattr(ctx, "repo", "")), len(task.get("surfaces") or [])
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
            "surfaces": list(task.get("surfaces") or []),
            "patterns": list(task.get("patterns") or []),
            "tier": dict(task.get("tier") or {}),
            **({"fix_attempts": fix_attempts} if fix_attempts is not None else {}),
            "build_budget_usd": build_budget_usd,
            "build_budget_source": build_budget_source,
        },
    )


def _style_fix(ctx: _Ctx, *, phase: str, task: str, result: Mapping[str, Any], build: dict[str, Any]) -> bool:
    """One `style_pass` over an approved build whose lint failed, then the checks again.

    The seat is a model and no reviewer sees its edit, so it is bounded three
    ways. Its patch passes the coverage floor `_trim_phase` uses, no collected
    test id may disappear. Only the paths it names are staged, because the
    checks have already written byproducts into this worktree. And the edit is
    recorded as an evidence row, for whoever reads the gate. True when the patch
    applied, held the floor and was committed; `build` then carries the fresh
    check results and `result`'s patch the folded diff. False restores the
    worktree, for a fall back to a revise.
    """
    worktree = ctx.task_worktree(phase, task)
    before = _collected_ids(worktree)
    if before is None:
        return False  # no floor can be measured, so no model call is worth buying
    patch = str((result.get("build") or {}).get("patch") or "")
    edit = ctx.runner.run(
        role="style_pass",
        tier="standard",
        schema=_STYLE_PASS_SCHEMA,
        task=task,
        wait_if_paused=lambda: _wait_if_paused(ctx),
        context=list(ctx.cartridge.get("context") or []),
        prompt=(
            f"{_CHECK_FIX_PROMPT}\n\nTicket: {task}\n\nFailing checks:\n{check_feedback(build['checks'])}"
            f"\n\nApproved diff:\n{patch[:PATCH_FOR_VALIDATION_CHARS]}"
        ),
    ).get("patch")
    if not str(edit or "").strip():
        return False
    if not apply_patch(str(edit), worktree)[0]:
        return False
    after = _collected_ids(worktree)
    if after is None or not coverage_floor_holds(before, after):
        _git("-C", str(worktree), "reset", "--hard", "-q")
        _git("-C", str(worktree), "clean", "-fdq")
        build["evidence"].append({"check": "style_pass", "output": "refused (coverage floor)"})
        return False
    paths = touched_paths(str(edit))
    staged, _ = _git(*_IDENTITY, "-C", str(worktree), "add", "-A", "--", *paths)
    if not staged or not _git(*_IDENTITY, "-C", str(worktree), "commit", "-q", "-m", "style pass")[0]:
        return False
    _, folded = _git("-C", str(worktree), "diff", ctx.phase_branch(phase), "HEAD")
    build_field = result.get("build")
    if isinstance(build_field, dict) and folded:
        build_field["patch"] = folded + "\n"
    results = run_checks(
        worktree, ctx.checks, env=_lane_checks_env(ctx, phase, task), **_checks_log(ctx, phase, task)
    )
    build["checks"] = results
    build["check_scope"] = {"reason": "style pass reran the full list", "tests_run": _tests_run(results)}
    # Only `checks:` rows are replaced; `verify:` rows stay as run before the style edit.
    build["evidence"] = [
        *(row for row in build["evidence"] if not str(row.get("check")).startswith("checks:")),
        {"check": "style_pass", "output": f"edit applied after approval, not reviewed: {', '.join(paths)}"},
        *checks_evidence(results),
        {"check": "checks:scope", "output": f"full list rerun after a style pass; tests_run={_tests_run(results)}"},
    ]
    build["quarantine"] = quarantine_reason(results)
    return True


def _refix(
    ctx: _Ctx,
    by_id: Mapping[str, Mapping[str, Any]],
    *,
    phase: str,
    task: str,
    result: dict[str, Any],
    build: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
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
    spent = float((result.get("fix_loop") or {}).get("attempts") or 1)
    item = {**(by_id.get(task) or {}), "id": task}
    routes: list[str] = []
    while build.get("quarantine") and "checks" in build:
        left = limit + 1 - spent
        # One unreviewed style edit per task: a second failure goes to a reviewed revise.
        style_open = "style_pass" in ctx.bound and "style_pass" not in routes
        route = refix_route(build["checks"], ctx.checks, attempts_left=left, style_bound=style_open)
        if route == "quarantine":
            break
        if route == "style_pass" and _style_fix(ctx, phase=phase, task=task, result=result, build=build):
            spent += 1
            routes.append("style_pass")
            continue
        failed = {
            "run": ctx.run_id,
            "phase": phase,
            "reason": f"approved, then failed its configured checks:\n{check_feedback(build['checks'])}",
        }
        carried = _carry_forward(
            item.get("body") or "",
            [*(item.get("attempts") or []), failed],
            str((result.get("build") or {}).get("patch") or ""),
            limit=PATCH_FOR_VALIDATION_CHARS,
        )
        body, permitted = _permit_outside_surfaces(
            carried, item.get("surfaces") or [], str(build.get("quarantine") or "")
        )
        retried, _, failures = invoke_graphs(
            [
                _lifecycle_invocation(
                    ctx, {**item, "surfaces": permitted}, body=body, fix_attempts=max(int(left) - 1, 0)
                )
            ],
            specs=ctx.specs,
            runner=ctx.runner,
            run_id=f"{ctx.run_id}:{phase}:refix{len(routes) + 1}",
            max_parallel=1,
        )
        why = failures[0] if failures else None
        if why is None and retried[0].get("failed_node") is not None:
            why = f"node '{retried[0]['failed_node']}' failed"
        if why is None:
            why = _unapproved(retried[0])
        if why is not None:
            build["evidence"].append({"check": "fix loop re-entry", "output": f"not approved: {why}"})
            break
        result = retried[0]
        result.setdefault("initiative", ctx.initiative_id)
        result.setdefault("phase", phase)
        _save_result(ctx, result, phase=phase, task=task)
        spent += float((result.get("fix_loop") or {}).get("attempts") or 1)
        routes.append("revise")
        names = ", ".join(str(c["name"]) for c in build["checks"] if not c.get("passed"))
        _release_task(ctx, phase, task)
        _drop_lane(ctx, phase, task)
        _git("-C", str(ctx.repo), "branch", "-D", ctx.scratch_branch(task))
        build = _build_task(ctx, phase=phase, task=task, result=result, verify=_verify_of(by_id, task))
        build["evidence"].append(
            {"check": "fix loop re-entry", "output": f"revise after {names}: attempts spent {spent:g} of {limit + 1}"}
        )
    return result, ({**build, "refix": routes} if routes else build)


def _unapproved(result: Mapping[str, Any]) -> str | None:
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
        isinstance(item, Mapping) and item.get("kind") == "draft_pr_create"
        for item in result.get("proposals") or []
    ):
        return None
    loop = result.get("fix_loop") or {}
    stopped = str(loop.get("stopped") or "") or "the reviewers did not approve the build"
    attempts = loop.get("attempts")
    counted = f" after {attempts} build attempt{'s' if attempts != 1 else ''}" if attempts else ""
    arbitration = result.get("arbitration")
    arbitration = arbitration if isinstance(arbitration, Mapping) else {}
    if arbitration.get("verdict"):
        verdict = str(arbitration["verdict"])
        reasoning = str(arbitration.get("reasoning") or "").split(". ", 1)[0].split(".", 1)[0]
        detail = f", sided_with {arbitration.get('sided_with')}: {reasoning}"
    else:
        verdict = str((result.get("review") or {}).get("verdict") or "") or "none recorded"
        objections = (result.get("adversary") or {}).get("objections") or []
        detail = f": {objections[0].get('claim') or ''}" if objections else ""
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


def checked_work_state(value: Any) -> str:
    """`value` when it is one of WORK_STATES; anything else is refused, never read as the default."""
    if value not in WORK_STATES:
        raise ValueError(f"provider profile 'work_state' must be one of {', '.join(WORK_STATES)}, not {value!r}")
    return str(value)


def work_state_of(profile: Mapping[str, Any]) -> str:
    """The profile's `work_state`: which side owns a task's state. An absent key means store when `storage_url` is Postgres, else files."""
    if "work_state" in profile:
        return checked_work_state(profile["work_state"])
    if str(profile.get("storage_url") or "").startswith(POSTGRES_URL_PREFIXES):
        return "store"
    return "files"


def run_epic(
    *,
    initiative: Mapping[str, Any],
    repo: Path | str,
    cartridge: Mapping[str, Any],
    runner: Any,
    specs: Mapping[str, Any],
    run_id: str,
    date: str,
    max_parallel: int,
    ledger_path: Path | str,
    provider_profile: str,
    runs_dir: Path | str,
    worktree_root: Path | str,
    assume: str | None = None,
    fix_attempts: int | None = None,
    resume_from: str | None = None,
    keep_worktrees: bool = False,
    store: Store | None = None,
    epoch: int | None = None,
    lease_name: str | None = None,
    work_state: str = "files",
    base: str | None = None,
    retry: _StoreRetry | None = None,
    headroom_usd: Callable[[], float | None] | None = None,
    est_task_usd: float = 0.0,
    only_phases: Collection[str] | None = None,
    pool: PoolSetting | None = None,
) -> dict[str, Any]:
    """Drive a whole initiative: every phase, in dependency order, landing nothing.

    With a `pool`, each task build leases its worktree from the host's warm pool instead of creating one,
    and the pool is trimmed to `pool.keep` idle entries at the end. Without it nothing changes.

    With `only_phases`, just those phases run. An unknown name refuses the run before any write. A parent outside
    the set counts complete when every task of it is met under `land_at`; a named phase under any other outside
    parent is recorded blocked.

    Phases run as lanes: a phase opens once its parent phases are complete, and `plan_launches` starts the
    ready tasks of every open phase, up to `max_parallel` or the usage `headroom_usd() / est_task_usd`.

    `initiative` is `core.workstore.read_initiative(...)` output — read by the
    CLI and passed in, because a driver that read the store itself would put the
    filesystem back inside the thing under test. `repo` is required: stacking is
    real branches in a real repository, and there is no honest way to fake that.

    Failure is continued-and-quarantined at BOTH grains. A task that fails its
    checks is set aside and its siblings still gate; a phase that does not meet
    its goal blocks its own dependents and nothing else. One task must not take
    a phase with it, and one phase must not take an initiative with it.

    Every exit — normal completion, an early return, or an exception raised
    anywhere in the phase loop — cleans up this run's worktrees in a
    `finally`: removed by default, or moved under `_kept/<run_id>` when
    `keep_worktrees` is set. The primitives live in `harness.worktree`; this
    only decides which one to call.

    The `store` is required: the run is recorded there as phases, tasks, attempts, gate
    decisions and ledger rows, and no per-phase manifest file is written. With an
    `epoch`, every leader-only write first asserts that epoch against the lease
    `lease_name` and is refused when it is stale.

    The writes at the end of a run (task mirrors, the pause clear, the exit record) are retried through
    `retry` across a lost store connection. The exit record is attempted last and always, with its own window.
    """
    if store is None:
        raise ValueError(
            "the epic driver needs a store: phases, tasks, attempts, gate decisions and ledger rows are recorded there"
        )
    work_state = checked_work_state(work_state)
    if only_phases is not None:
        known = {str(i["phase"]) for i in initiative.get("items") or [] if i.get("phase")}
        if unknown := sorted(set(only_phases) - known):
            raise ValueError(f"unknown phase(s) {', '.join(unknown)}; the initiative's phases are {', '.join(sorted(known))}")
    retry = retry or _StoreRetry.for_store(store)
    repo = Path(repo).expanduser()
    ctx: _Ctx | None = None
    result: dict[str, Any] | None = None
    # Bound before the `try` so the `finally` can build a partial result from whatever phases finished.
    phases: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    stacks_rebased = 0
    paused_until: str | None = None
    try:
        try:
            # `utf-8-sig` also strips a leading BOM, which would otherwise survive
            # into the first command's name and cmd and never resolve as a shell
            # command. Either way a malformed file degrades to no repo checks,
            # never to a run that dies on somebody else's typo.
            agent_checks_text = (repo / ".agent-checks").read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            agent_checks_text = ""
        ctx = _Ctx(
            repo=repo,
            cartridge=cartridge,
            runner=runner,
            specs=specs,
            run_id=run_id,
            date=date,
            max_parallel=max_parallel,
            ledger_path=Path(ledger_path),
            provider_profile=provider_profile,
            runs_dir=Path(runs_dir),
            worktree_root=Path(str(worktree_root)).expanduser(),
            assume=assume,
            fix_attempts=fix_attempts,
            resume_from=resume_from,
            repo_checks=repo_checks(agent_checks_text),
            test_database=test_database_setting(cartridge),
            lane_workers=lane_workers_from(os.environ, os.cpu_count() or 1),
            pool=pool,
            store=store,
            epoch=epoch,
            lease_name=lease_name,
            work_state=work_state,
            retry=retry,
            initiative_id=str(initiative.get("id")),
            # Resolved before any phase starts, so a bad value refuses the run rather than a phase.
            land_at=resolve_land_at(cartridge, initiative.get("land_at")),
            # An unparented phase branches from `origin/<base>` after a fetch, read
            # once here so every phase in a run stacks on the same ground, whatever
            # branch the checkout has out.
            default_ref=resolve_default_ref(repo, base),
        )

        # The driver's own view of the work. `ready_tasks` answers from item state,
        # so an EXECUTED `state_move` has to be reflected here or the next phase's
        # tasks never become ready within this run. The arm remains the single
        # writer of the store on disk; this is the driver keeping its own copy
        # honest about what the arm just did.
        items = [dict(item) for item in initiative.get("items") or []]
        _mirror_read(ctx, items)

        parents = phase_parents(items)
        named = None if only_phases is None else frozenset(only_phases)
        seed: frozenset[str] = frozenset()
        if named is not None:
            seed = frozenset(outside_parents_complete(items, _read_store_rows(ctx), parents, named, ctx.land_at))
            parents = {p: ps for p, ps in parents.items() if p in named}
        waves, cyclic = compute_waves(parents if named is None else {p: ps & named for p, ps in parents.items()})
        _, binding = lane_bound(max_parallel, 0, headroom_usd() if headroom_usd else None, est_task_usd)
        print(
            f"  waves: {' | '.join(', '.join(w) for w in waves) or 'none'}; widest wave {widest_wave(waves)}; "
            f"lane bound {max_parallel} ({binding})"
        )
        unreached = {
            later: gap
            for later, gap in unreached_phases(items, declared_phases(initiative, items)).items()
            if named is None or later in named
        }
        for line in parent_lines(parents, unreached):
            print(line)
        if (warning := unreached_warning(unreached)) is not None:
            print(warning)

        def add_phase(record: dict[str, Any]) -> None:
            phases.append(record)
            _record_phase(ctx, record)
            if (line := blocked_line(record)) is not None:
                print(line)
            for line in awaiting_land_lines(record, run_id, repo):
                print(line)

        for phase in cyclic:
            add_phase(
                {
                    "phase": phase,
                    "status": "blocked",
                    "reason": (
                        "phase dependency cycle: the task DAG is acyclic but the phase graph it "
                        "induces is not, so no order over these phases exists"
                    ),
                }
            )

        scope: frozenset[str] | None = None
        if named is not None:
            for phase in sorted(parents):
                if missing := sorted(parents[phase] - named - seed):
                    add_phase(
                        {
                            "phase": phase,
                            "status": "blocked",
                            "parents": sorted(parents[phase]),
                            "reason": (
                                f"parent phase '{missing[0]}' is outside this run and its tasks are not all met "
                                f"under land_at {ctx.land_at}; dependents do not run"
                            ),
                        }
                    )
            scope = frozenset(p for p in parents if p not in {str(r["phase"]) for r in phases})

        tally = _Tally()
        _run_lanes(
            ctx, initiative=initiative, items=items, parents=parents, waves=waves, add_phase=add_phase,
            phases=phases, tasks=tasks, quarantined=quarantined, proposals=proposals, tally=tally,
            headroom_usd=headroom_usd, est_task_usd=est_task_usd, seed_complete=seed, scope=scope,
        )
        stacks_rebased, paused_until = tally.stacks_rebased, tally.paused_until
        rank = {p: n for n, p in enumerate([*cyclic, *(p for w in waves for p in w)])}
        phases.sort(key=lambda r: rank.get(str(r["phase"]), len(rank)))

        # A pause too: tasks approved before it can have lost their branch. The audit calls no model.
        _audit_approved_kept(ctx, {str(i["id"]): i for i in items}, tasks, quarantined)
        result = _build_result(
            run_id=run_id, date=date, initiative_id=ctx.initiative_id, phases=phases, tasks=tasks,
            quarantined=quarantined, proposals=proposals, stacks_rebased=stacks_rebased,
            paused_until=paused_until, repo=repo, land_at=ctx.land_at, waves=waves, lane=tally.lane,
            lane_databases=ctx.lane_drops if ctx.test_database else None,
        )
        return result
    finally:
        # Every exit — the return above, a raise from anywhere in this try, or
        # a signal delivered as KeyboardInterrupt or SystemExit — lands here.
        # A failed pause clear, even a bug in it, must not skip the exit record, the last write given up on.
        try:
            _clear_pause_if_held(store, run_id, lease_name or LEASE_NAME, epoch, retry)
        finally:
            # Once, after the phase loop has drained: a phase's threads stay open while any other phase can still use them.
            close_error = _close_runner(ctx.runner) if ctx is not None else None
            # A raise leaves no result, so the outcome is built from the phases that finished. A failure
            # building it is logged and must not replace the exception already in flight.
            if result is None and ctx is not None:
                try:
                    result = _build_result(
                        run_id=run_id, date=date, initiative_id=ctx.initiative_id, phases=phases, tasks=tasks,
                        quarantined=quarantined, proposals=proposals, stacks_rebased=stacks_rebased,
                        paused_until=paused_until, repo=repo,
                        land_at=ctx.land_at, lane_databases=ctx.lane_drops if ctx.test_database else None,
                    )
                except Exception as exc:
                    _log.warning("partial result for %s not built: %s: %s", run_id, type(exc).__name__, exc)
            # A return leaves no exception in flight here; a raise, a signal or an exit leaves one.
            exit_status = "ok" if sys.exc_info()[0] is None and close_error is None else "error"
            _record_run_exit(store, run_id, exit_status, retry, result)
            _publish_run_exit(run_id, ctx.initiative_id if ctx is not None else "", exit_status)
        # Nothing before `ctx` exists can have made a worktree, so there is nothing to do yet.
        if ctx is not None:
            run_dir = ctx.worktree_root / ctx.run_id
            _settle_pool(ctx, keep_worktrees=keep_worktrees)
            if keep_worktrees:
                # `keep_worktree` moves ONE named worktree to
                # `_kept/<run_id>/<name>`; run_dir's own name is the run id,
                # so calling it on run_dir directly would double that segment
                # into `_kept/<run_id>/<run_id>`. Called once per phase
                # directory instead, each move lands where work-shape.md §6
                # says a kept run lands: `_kept/<run_id>/<phase>`.
                if run_dir.is_dir():
                    for phase_dir in sorted(run_dir.iterdir()):
                        keep_worktree(ctx.repo, phase_dir, ctx.worktree_root, ctx.run_id)
                    if not any(run_dir.iterdir()):
                        run_dir.rmdir()
            else:
                remove_worktree(ctx.repo, run_dir)
                # After the removal, into the list the result already holds, so a pending or failed entry is the cleanup list.
                for phase, task in sorted(ctx.lane_names):
                    _drop_lane(ctx, phase, task)
        # Raised last, so a failing close still leaves the exit record written and the worktrees cleaned up.
        if close_error is not None:
            raise close_error


@dataclass
class _Tally:
    stacks_rebased: int = 0
    paused_until: str | None = None
    lane: dict[str, Any] = field(default_factory=dict)


def _open_view(
    items: Sequence[Mapping[str, Any]],
    parents: Mapping[str, set[str]],
    complete: set[str],
    launched: set[str],
    failed: Collection[str] = (),
) -> list[dict[str, Any]]:
    """`items` as the planner sees them: a ready task is held while its phase has an incomplete parent or it already launched.

    A task whose needs are all merged is not held for an incomplete parent, unless a parent has finished without
    meeting its goal (`failed`): its dependents do not run.
    """
    closed = {p for p, ps in parents.items() if not ps <= complete}
    shut = {p for p, ps in parents.items() if ps & set(failed)}
    by_id = {str(i["id"]): i for i in items}

    def state(item: Mapping[str, Any]) -> Any:
        if item.get("state") != "ready":
            return item.get("state")
        if str(item["id"]) in launched:
            return "in_progress"
        return "blocked" if str(item.get("phase")) in shut or (str(item.get("phase")) in closed and not _needs_merged(item, by_id)) else "ready"

    return [{**item, "state": state(item)} for item in items]


def _needs_merged(item: Mapping[str, Any], by_id: Mapping[str, Mapping[str, Any]]) -> bool:
    """Does the task have needs, each one done, dropped, or approved and merged with a stored commit?

    Such a task starts before its phase's parents are complete. A need outside this initiative reads as met here;
    the planner checks it. A task with no needs waits for the whole parent phase.
    """
    needs = [str(n) for n in item.get("needs") or []]
    return bool(needs) and all(
        n not in by_id or _need_met(by_id[n]) for n in needs
    )


def _need_met(need: Mapping[str, Any]) -> bool:
    if need.get("state") in ("done", "dropped"):
        return True
    return need.get("state") == "approved" and bool(need.get("merged")) and bool(need.get("merged_commit"))


def _run_lanes(
    ctx: _Ctx,
    *,
    initiative: Mapping[str, Any],
    items: list[dict[str, Any]],
    parents: Mapping[str, set[str]],
    waves: Sequence[Sequence[str]],
    add_phase: Callable[[dict[str, Any]], None],
    phases: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    quarantined: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    tally: _Tally,
    headroom_usd: Callable[[], float | None] | None,
    est_task_usd: float,
    seed_complete: frozenset[str] = frozenset(),
    scope: frozenset[str] | None = None,
) -> None:
    """Run phases as lanes until nothing runs and the planner starts nothing; every phase ends with one record.

    `seed_complete` names parents outside the run that already count complete. With a `scope`, a ready task of a
    phase outside it is held, so only those phases launch.
    """
    order = [p for w in waves for p in w]
    wave_of = {p: n + 1 for n, w in enumerate(waves) for p in w}
    phase_of = {str(i["id"]): str(i.get("phase") or "") for i in items}
    running: dict[Future[dict[str, Any]], tuple[str, frozenset[str], int]] = {}
    lane_of: dict[str, int] = {}
    acc: dict[str, dict[str, Any]] = {}
    launched: set[str] = set()
    complete: set[str] = set(seed_complete)
    recorded = {str(r["phase"]) for r in phases}
    serialised: dict[str, str] = {}
    why_stopped: list[str] = []

    def settle(phase: str, record: dict[str, Any]) -> None:
        record = {**record, "wave": wave_of.get(phase), "lane": lane_of.get(phase)}
        if record["status"] == "awaiting_land" and adopt_done(phase):
            # The store keeps the first row of a phase, so a phase done elsewhere is recorded complete the first time.
            # Only an awaiting_land record is rewritten: a partial one carries a verdict this check must not overrule.
            record = {k: v for k, v in record.items() if k != "awaiting_tasks"}
            record.update(status="complete", reason="every task in the phase is done in the store")
        if phase in serialised:
            record["serialised"] = serialised[phase]
        add_phase(record)
        recorded.add(phase)
        if record["status"] == "complete":
            complete.add(phase)

    def adopt_done(parent: str) -> bool:
        """True after adopting the rows of `parent`, when every one of its tasks is done in the store."""
        rows = _read_store_rows(ctx)
        with ctx.items_lock:
            mine = [i for i in items if i.get("phase") == parent]
            fresh = _store_fresh(mine, rows)
            if not fresh or any(i["state"] not in ("done", "dropped") for i in fresh):
                return False
            for item, new in zip(mine, fresh, strict=True):
                item.update({k: new[k] for k in ("state", "merged", "merged_commit")})
        return True

    def parent_block(phase: str) -> dict[str, Any] | None:
        bad = next((p for p in sorted(parents[phase]) if p in recorded and p not in complete), None)
        if bad is None:
            return None
        parent_record = next((r for r in phases if r.get("phase") == bad), {})
        waiting = len(parent_record.get("awaiting_tasks") or [])
        return {
            "phase": phase,
            "status": "blocked",
            "parents": sorted(parents[phase]),
            "reason": (
                f"parent phase '{bad}' is awaiting the land of {waiting} approved task(s)"
                if parent_record.get("status") == "awaiting_land"
                else f"parent phase '{bad}' did not meet its goal; dependents do not run"
            ),
        }

    def fold(fut: Future[dict[str, Any]]) -> None:
        phase, _ids, _lane = running.pop(fut)
        try:
            record = fut.result()
        except LimitStop as exc:
            tally.paused_until = exc.detail.split("resets", 1)[-1].strip()
            why_stopped.append(f"account session limit, resets {tally.paused_until}")
            return
        phase_tasks = record.pop("task_records")
        tasks.extend(phase_tasks)
        for t, command in _unlanded(phase_tasks, ctx.run_id, ctx.repo):
            send_to_courier(f"coxswain://task/{t['id']}", "chair", command)
        quarantined.extend(record.pop("quarantined"))
        proposals.extend(record.pop("batch"))
        tally.stacks_rebased += 1 if record.get("rebased") else 0
        acc[phase] = {**record, "rebased": bool(record.get("rebased") or acc.get(phase, {}).get("rebased"))}
        if record.get("stopped"):
            print(f"  run stopped: {record['stopped']}; later phases not run")
            why_stopped.append(f"run stopped: {record['stopped']}")

    with ThreadPoolExecutor(max_workers=max(1, len(parents))) as pool:

        def launch(phase: str, ids: frozenset[str], only: frozenset[str] | None) -> None:
            used = {lane for *_, lane in running.values()}
            lane_of.setdefault(phase, next(n for n in range(1, len(used) + 2) if n not in used))
            launched.update(ids)
            future = pool.submit(
                _run_phase, ctx, initiative=initiative, phase=phase,
                parents=tuple(sorted(parents[phase])), items=items, only=only,
            )
            ctx.launched[phase] = future
            running[future] = (phase, ids, lane_of[phase])

        while True:
            hr = headroom_usd() if headroom_usd else None
            by_phase = {p: ids for p, ids, _ in running.values()}
            # Replan until settling a phase changes nothing, so a phase that just completed opens its children now.
            while True:
                with ctx.items_lock:
                    view = _open_view(items, parents, complete, launched, {p for p in recorded if p not in complete})
                    if scope is not None:
                        view = [
                            {**i, "state": "blocked"} if i.get("state") == "ready" and str(i.get("phase")) not in scope else i
                            for i in view
                        ]
                    plan = plan_launches(
                        view, by_phase, ctx.max_parallel, initiative.get("foreign") or {}, hr, est_task_usd,
                        ctx.land_at,
                    )  # fmt: skip
                    cand = {
                        str(t["phase"])
                        for t in workstore.ready_tasks(view, foreign=initiative.get("foreign") or {}, land_at=ctx.land_at)
                    }
                before = len(recorded)
                idle: list[str] = []
                for tid, why in sorted(plan.held.items()):
                    if " waits for phase " in why and phase_of[tid] not in serialised:
                        serialised[phase_of[tid]] = why
                        print(f"  {why}")
                for phase in sorted(acc):
                    if phase not in by_phase and phase not in cand:
                        settle(phase, acc.pop(phase))
                for phase in order:
                    if phase in recorded or phase in lane_of or why_stopped:
                        continue
                    held = next(
                        (w for t, w in sorted(plan.held.items()) if phase_of[t] == phase and "is quarantined" in w), None
                    )
                    if (block := parent_block(phase)) is not None:
                        settle(phase, block)
                    elif phase not in cand and parents[phase] <= complete:
                        if held is not None:
                            settle(phase, {"phase": phase, "status": "blocked", "parents": sorted(parents[phase]),
                                           "reason": f"{held}, so the phase cannot run; dependents do not run"})
                        else:
                            idle.append(phase)
                if len(recorded) == before:
                    # Open phases with nothing ready get one call for their record, only once the view is current.
                    for phase in idle if not why_stopped else []:
                        launch(phase, frozenset(), None)
                    break
            free, binding = lane_bound(ctx.max_parallel, sum(len(ids) for ids in by_phase.values()), hr, est_task_usd)
            tally.lane = {"free": free, "binding": binding}
            if not why_stopped:
                in_flight = {p for p, _, _ in running.values()}
                for phase, ids in plan.start.items():
                    if phase not in in_flight:
                        launch(phase, frozenset(ids), frozenset(ids))
            if not running:
                break
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                fold(future)

    for phase in order:
        if phase in recorded:
            continue
        if phase in acc:
            settle(phase, acc.pop(phase))
        elif why_stopped:
            continue  # a stopped run leaves later phases without a record, as before
        elif (block := parent_block(phase)) is not None:
            settle(phase, block)
        else:
            reason = f"held by {tally.lane.get('binding')}: {tally.lane.get('free')} free slot(s)"
            settle(phase, {"phase": phase, "status": "paused", "parents": sorted(parents[phase]), "reason": reason})


def _quarantine_task(
    ctx: _Ctx,
    by_id: Mapping[str, dict[str, Any]],
    *,
    phase: str,
    task: str,
    reason: str,
    kind: Literal["refused", "no_work", "unverified", "infra", "rescue_failed", "stale_base"],
    detail: str | None = None,
    result: Mapping[str, Any] | None = None,
    cause: tuple[str, str] | None = None,
    approved_is_open: bool = False,
) -> dict[str, Any]:
    """Build a task's quarantine entry AND leave a record on its own work item.

    The phase record is the run's memory; the item file is the task's own, so
    the next run — which may not even resume this one — still sees why the
    last attempt didn't land. A task built with no file behind it (as tests
    do) or a store that refuses the write loses the item-side memory, never
    the quarantine entry itself.

    `kind` is the closed set from docs/design/observed-record.md §3. `infra`
    reaches here three ways: the caller's own branch at the `_open_phase_worktree`
    failure site, which marks the phase `phase_failed_to_start` before this
    function ever sees a task; `_execute`, when the apply arm itself raises
    rather than reports; and a non-build node raising mid-round, where the patch
    a later node was handed is kept rather than discarded. `unverified` is an
    approved patch the validator would not sign off, kept for the same reason.
    Both carry `patch_kept: True` on both the entry and the attempt. `infra` from
    `_execute` names the BUILD's already-approved patch already on the task
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
    stale = _fenced(ctx)
    if stale is not None:
        # A stale leader records no attempt anywhere, so it is not an attempt either.
        return {"id": task, "phase": phase, "grain": "task", "reason": f"{stale}; not recorded: {reason}", "kind": "no_work"}
    if termination.is_terminating():
        # Wrong belief: a failure seen after SIGTERM is a failed build. It is the kill, so it is no attempt and the task stays ready.
        return {"id": task, "phase": phase, "grain": "task", "reason": f"run terminated by signal; not recorded: {reason}", "kind": "no_work"}
    # Before any write, so a `LimitStop` from the classifier leaves nothing half recorded.
    judged = (cause or _cause_of(ctx.runner, kind, reason, result, task)) if ctx.store is not None else None
    patch_kept = kind in ("unverified", "infra")
    entry: dict[str, Any] = {"id": task, "phase": phase, "grain": "task", "reason": reason, "kind": kind}
    if patch_kept:
        entry["patch_kept"] = True
    ts = _now()
    path = (by_id.get(task) or {}).get("path")
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
        cause, cause_why = judged
        store = ctx.store

        def attempt() -> None:
            # The count and the insert retry as one unit, so a retry recounts rather than reusing a stale seq.
            seq = _next_attempt_seq(store, ctx.run_id, task)
            store.record_attempt(
                ctx.run_id, task, seq, phase, kind, detail or reason, ts, epoch=ctx.epoch, cause=cause, cause_why=cause_why
            )

        ctx.retry.write(attempt, _END_WRITE_WINDOW_S)
        _record_task_cause(ctx, phase, task, cause, cause_why)
    # A merge conflict leaves the row at approved with its work unlanded; approved is not always finished.
    _quarantine_row(ctx, phase, task, approved_is_open=approved_is_open or judged == ("harness", "merge conflict"))
    return entry


def _cause_of(runner: Any, kind: str, reason: str, result: Mapping[str, Any] | None, task: str) -> tuple[str, str]:
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


def _record_task_cause(ctx: _Ctx, phase: str, task: str, cause: str, cause_why: str) -> None:
    """Put the cause on this run's saved result, which mirrors it into the store row: file and row stay equal.

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
        _save_result(ctx, {**saved, "cause": cause, "cause_why": cause_why}, phase=phase, task=task)
    except Exception as exc:
        _log.warning("cause for %s/%s/%s not recorded on the task record: %r", ctx.run_id, phase, task, exc)


def _without_cause(saved: Mapping[str, Any]) -> dict[str, Any]:
    """A saved result as a later run reuses it. Its cause names the run that quarantined it, not this one."""
    return {k: v for k, v in saved.items() if k not in ("cause", "cause_why")}


def _next_attempt_seq(store: Store, run_id: str, task: str) -> int:
    """The attempts already stored for (run, task), so a rerun of the same run id appends rather than collides."""
    mark = store.conn.dialect.placeholder
    row = store.conn.query_one(f"SELECT COUNT(*) FROM attempts WHERE run_id = {mark} AND task_id = {mark}", (run_id, task))
    return 0 if row is None else int(row[0])


LEASE_NAME = "chair"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _mirror_by(ctx: _Ctx) -> str:
    return f"epic-driver:{ctx.run_id}"


def _file_times(items: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Task id to the ISO mtime of its work file; an item with no readable file has no entry."""
    times: dict[str, str] = {}
    for item in items:
        with contextlib.suppress(OSError):
            if item.get("path"):
                times[str(item["id"])] = datetime.fromtimestamp(Path(str(item["path"])).stat().st_mtime, UTC).isoformat()
    return times


def _upsert_row(store: Store, row: Mapping[str, Any]) -> None:
    upsert_work_item(
        store.conn, row["initiative"], row["task_id"], row["phase"], row["state"], row["needs"],
        row["updated_at"], row["updated_by"],
    )  # fmt: skip


def _repair_file_state(item: Mapping[str, Any], new_state: str) -> bool:
    """Rewrite only the `state:` line of the item's work file, bytes otherwise kept. False when it could not."""
    if not item.get("path"):
        return False
    path = Path(str(item["path"]))
    try:
        text = path.read_bytes().decode("utf-8")
        repaired = work_mirror.set_frontmatter_state(text, new_state)
        if repaired is None:
            return False
        if repaired != text:
            path.write_bytes(repaired.encode("utf-8"))
    except (OSError, UnicodeError):
        return False
    return True


def _mirror_read_store(ctx: _Ctx, items: Sequence[dict[str, Any]], rows: Sequence[Mapping[str, Any]]) -> None:
    """work_state: store. A row wins over its file: the file's state line is repaired and `items` follows the row.

    `items` is the driver's own copy, so the ready and needs checks that read it see the store's state. A task with
    no row is read from its file and upserted, as under files.
    """
    by_task = {row["task_id"]: row for row in rows}
    times = _file_times(items)
    for item in items:
        task, file_state = item["id"], item["state"]
        row = by_task.get(task)
        row_state = None if row is None else row["state"]
        decision = work_mirror.authority_decision("store", file_state, row_state)
        if decision == work_mirror.USE_STORE_REWRITE_FILE:
            if _repair_file_state(item, row_state):
                _log.warning("work file state rewritten from the store: task=%s old=%s new=%s", task, file_state, row_state)
            else:
                _log.warning("work file state not rewritable, store state used: task=%s file=%s", task, file_state)
            item["state"] = row_state
        elif decision == work_mirror.USE_FILE_AND_UPSERT:
            new = work_mirror.item_row(ctx.initiative_id, item, times.get(str(task)) or _now(), _mirror_by(ctx))
            _upsert_row(ctx.store, new)
        if row is not None:
            # A merge and its handoff live only in the row, so `ready_tasks` sees a merged parent in any run.
            item["merged"] = bool(row.get("merged"))
            item["merged_commit"] = row.get("merged_commit")
            item["handoff"] = row.get("handoff")


def _mirror_read(ctx: _Ctx, items: Sequence[dict[str, Any]]) -> None:
    """Mirror the files' state into work_items. Under work_state files the files stay authoritative: `items` is never touched.

    Under work_state store a stored row wins: see `_mirror_read_store`. A ctx with no work_state reads as files.
    A store error is logged and swallowed here, at the edge, so it can never fail the run.
    """
    if ctx.store is None:
        return
    try:
        # Not a module-level import: harness/__init__ imports this module, so a top-level store_read import
        # loads store_traces early and `python -m harness.store_traces` then warns a second line on stderr.
        from harness import store_read

        rows = store_read.work_items(ctx.store.conn, ctx.initiative_id)
        if getattr(ctx, "work_state", "files") == "store":
            _mirror_read_store(ctx, items, rows)
            return
        upserts, disagreements = work_mirror.plan_mirror(
            ctx.initiative_id, items, rows, _file_times(items), _mirror_by(ctx), fallback_time=_now()
        )
        for row in upserts:
            _upsert_row(ctx.store, row)
        for d in disagreements:
            _log.warning(
                "work_items disagrees with the file: initiative=%s task=%s file_state=%s store_state=%s updated_by=%s",
                d["initiative"], d["task_id"], d["file_state"], d["store_state"], d["store_updated_by"],
            )  # fmt: skip
    except Exception as exc:
        _log.warning("work_items mirror (read) failed for %s: %s: %s", ctx.initiative_id, type(exc).__name__, exc)


def _store_authoritative(ctx: _Ctx) -> bool:
    return ctx.store is not None and getattr(ctx, "work_state", "files") == "store"


def _read_store_rows(ctx: _Ctx) -> list[Mapping[str, Any]] | None:
    """The initiative's work_items rows as the store holds them now; None under files or when the read fails.

    The one store read of the done-wins check. A failure is logged and reads as no rows, as `_mirror_read` does.
    """
    if not _store_authoritative(ctx):
        return None
    try:
        from harness import store_read  # not module level, for the reason given in `_mirror_read`

        return list(store_read.work_items(ctx.store.conn, ctx.initiative_id))
    except Exception as exc:
        _log.warning("work_items not re-read for %s: %s: %s", ctx.initiative_id, type(exc).__name__, exc)
        return None


def _store_fresh(items: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Copies of `items` where a task whose row says done takes the row's state, merged flag and merged commit."""
    done = {str(r["task_id"]): r for r in rows or () if r.get("state") == "done"}
    return [
        {**item, "state": "done", "merged": bool(done[k].get("merged")), "merged_commit": done[k].get("merged_commit")}
        if (k := str(item["id"])) in done
        else dict(item)
        for item in items
    ]


def _store_first(ctx: _Ctx, item: dict[str, Any], state: str) -> str | None:
    """work_state: store. Move `item` to `state` in the store, compare-and-set on the state the mover read.

    None when the store accepted, so the driver writes the file line itself. Otherwise the refusal reason: nothing else runs.
    A mismatch adopts the store's state into `item`, as `_mirror_read_store` does. A store error refuses the move.
    """
    from harness import store_work_state  # not module level: see `_mirror_read`

    task, expected = item.get("id"), str(item["state"])
    try:
        written = store_work_state.set_state(
            ctx.store.conn, ctx.initiative_id, str(task), state, _mirror_by(ctx), item.get("phase"), _now(), expected=expected
        )
    except Exception as exc:
        reason = f"state move refused, store write failed: {type(exc).__name__}: {exc}"
        _log.warning("%s: task=%s", reason, task)
        return reason
    if not isinstance(written, store_work_state.Mismatch):
        return None
    _, why = work_mirror.check_expected_state(expected, written.current)
    reason = f"state move refused: {why}"
    _log.warning("%s: task=%s", reason, task)
    if written.current is not None:
        item["state"] = written.current
    return reason


def row_should_quarantine(work_state: str, current_state: str | None, *, approved_is_open: bool = False) -> bool:
    """True when a quarantine moves the store row: store work state, a row that exists, and not finished or already quarantined.

    `approved_is_open` is for a merge into the phase branch that failed: the row reads approved but its work never landed.
    """
    finished = ("done", "quarantined") if approved_is_open else ("approved", "done", "quarantined")
    return work_state == "store" and current_state is not None and current_state not in finished


def _git_stdout(*args: str, timeout: int = 120, ok_codes: tuple[int, ...] = (0,)) -> str | None:
    """Edge. Stdout of one git command, byte for byte; None on an exit outside `ok_codes`, a timeout or undecodable output.

    `_git` returns stderr in place of stdout when stderr is non-empty, so it cannot carry a NUL-separated listing.
    `git grep` exits 1 for no match, so its callers pass ok_codes=(0, 1).
    """
    try:
        proc = subprocess.run(["git", *args], capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError, UnicodeDecodeError):
        return None
    return proc.stdout if proc.returncode in ok_codes else None


def _default_branch_paths(ctx: _Ctx) -> frozenset[str] | None:
    """Edge. Repo-relative file paths on `ctx.default_ref`, the ref `resolve_default_ref` produced after its fetch.

    Cached on `ctx` by the commit the ref names, so a phase reuses the listing until the ref moves.
    None when the ref or its listing cannot be read; that is not cached.
    """
    repo = ("-C", str(ctx.repo))
    named = _git_stdout(*repo, "rev-parse", "--verify", f"{ctx.default_ref}^{{commit}}")
    if named is None:
        return None
    sha = named.strip()
    with ctx.items_lock:
        if sha not in ctx.surface_paths:
            listing = _git_stdout(*repo, "ls-tree", "-r", "-z", "--name-only", sha)
            if listing is None:
                return None
            ctx.surface_paths[sha] = frozenset(p for p in listing.split("\0") if p)
        return ctx.surface_paths[sha]


def _default_branch_commit(ctx: _Ctx) -> str:
    """Edge. The commit `ctx.default_ref` names; empty when it cannot be read."""
    named = _git_stdout("-C", str(ctx.repo), "rev-parse", "--verify", f"{ctx.default_ref}^{{commit}}")
    return (named or "").strip()


def _without_correction_lint(ticket: Mapping[str, Any], original: Mapping[str, Any]) -> dict[str, Any]:
    """`ticket` without its `correction:` lint entries.

    A correction quotes the tree at one commit, so it goes stale once the code or the body changes. It is
    recomputed at each launch and handed to the builder, never stored.
    """
    kept = [n for n in ticket.get("lint") or [] if not str(n).startswith("correction:")]
    rest = {k: v for k, v in ticket.items() if k != "lint"}
    return {**rest, "lint": kept} if kept or "lint" in original else rest


def _working_tree_paths(root: Path) -> frozenset[str]:
    """Edge. Repo-relative file paths in the working tree of `root`; the fallback when the branch listing is unreadable."""
    return frozenset(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if ".git" not in p.relative_to(root).parts and p.is_file()
    )


def _surface_refusal_reason(malformed: Sequence[str]) -> str:
    """The reason a task is refused for surfaces that are absolute, reach outside the repository, or are a glob."""
    return (
        f"malformed surfaces: {', '.join(malformed)}; a surface is a repository relative path "
        "with no glob and no .. segment."
    )


def _frontmatter_key_lines(key: str, value: Sequence[str]) -> list[str]:
    return yaml.safe_dump({key: list(value)}, default_flow_style=None, allow_unicode=True).rstrip().split("\n")


def _heal_surfaces(task: Mapping[str, Any], paths: frozenset[str]) -> dict[str, Any]:
    """Bare filenames resolved or left out with a note, notes moved to lint, then missing surfaces marked `(new)`.

    An unmarked bare name that no repository file shares is a new file at the repository root, so it is not
    handed to heal_bare_filenames, which would drop it; heal_new_markers marks it `(new)` as before.
    """
    names = {p.rsplit("/", 1)[-1] for p in paths}
    surfaces = [str(s) for s in task.get("surfaces") or []]
    root_new = [
        s for s in surfaces
        if "/" not in s and not s.strip().endswith("(new)") and s.strip() not in names
        and s.strip() and not any(c.isspace() or c in "*?[]" for c in s.strip()) and s.strip() != ".."
    ]
    resolved = heal_bare_filenames({**task, "surfaces": [s for s in surfaces if s not in root_new]}, paths)
    kept = {**resolved, "surfaces": [*(resolved.get("surfaces") or []), *root_new]}
    return heal_new_markers(notes_out_of_surfaces(kept), paths)


def _rewrite_frontmatter_surfaces(path: Path, surfaces: Sequence[str], notes: Sequence[str]) -> list[str]:
    """Replace the `surfaces` key and extend the `lint` key of the file's frontmatter; every other line is kept as it was.

    Returns the lint list now in the file, so a caller can give the store row the same notes.
    """
    lines = path.read_text(encoding="utf-8").split("\n")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if lines[0].strip() != "---" or end is None:
        raise ValueError(f"{path}: no frontmatter block")
    lint = list(dict.fromkeys([*((yaml.safe_load("\n".join(lines[1:end])) or {}).get("lint") or []), *notes]))
    front = lines[1:end]
    for key, value in (("surfaces", surfaces), ("lint", lint)):
        at = next((i for i, line in enumerate(front) if line.startswith(f"{key}:")), None)
        if at is None:
            front = [*front, *_frontmatter_key_lines(key, value)]
            continue
        stop = next((i for i in range(at + 1, len(front)) if not front[i].startswith((" ", "-"))), len(front))
        front = [*front[:at], *_frontmatter_key_lines(key, value), *front[stop:]]
    path.write_text("\n".join(["---", *front, *lines[end:]]), encoding="utf-8")
    return lint


def _write_healed_surfaces(ctx: _Ctx, task: str, surfaces: Sequence[str], notes: Sequence[str], path: str | None) -> None:
    """Persist a healed task's surfaces and new lint notes, so a relaunch reads them and does not heal again.

    A stale leader writes neither the file nor the row. Otherwise the file is rewritten first with the row's lint
    notes and the new ones added to its own, and the row gets that same list and the same surfaces, in either work
    state. So a note held only by the row, or only by the file, survives in both. A file that cannot be rewritten
    leaves the row as it was. Under work_state store a task with no file path is the one case where the row is
    written alone, since the row is the source of truth and there is no file to disagree with. Errors are logged,
    never raised.

    Functions checked for other writes of a task file's title, body or surfaces: `_rewrite_frontmatter_surfaces`
    (called only here), `_repair_file_state` (the `state:` line only), and `_apply_amend_directly`, whose only file
    write puts back the bytes read before a refused amend. The fix-loop re-scope and correction rounds amend a ticket
    through the `work_state_arm` `ticket_amend` call and do not write the file themselves, so none needed routing.
    """
    try:
        stale = _fenced(ctx)
    except Exception as exc:
        _log.warning("healed surfaces not written: task=%s: %s: %s", task, type(exc).__name__, exc)
        return
    if stale is not None:
        _log.warning("healed surfaces not written: task=%s: %s", task, stale)
        return
    row: Mapping[str, Any] | None = None
    if ctx.store is not None:
        try:
            from harness import store_queue  # not module level: see `_mirror_read`

            row = next((r for r in store_queue.read(ctx.store.conn, ctx.initiative_id) if r["task_id"] == task), None)
        except Exception as exc:
            _log.warning("healed surfaces: store row not read: task=%s: %s: %s", task, type(exc).__name__, exc)
        if row is None:
            _log.warning("healed surfaces not written to the store row: task=%s: no row", task)
    extra = dict((row or {}).get("extra") or {})
    row_lint = [str(n) for n in extra.get("lint") or []]
    lint = list(dict.fromkeys([*row_lint, *notes]))
    if path:
        try:
            lint = _rewrite_frontmatter_surfaces(Path(path), surfaces, lint)
        except Exception as exc:
            _log.warning("healed surfaces not written to the task file: task=%s: %s: %s", task, type(exc).__name__, exc)
            _log.warning("healed surfaces not written to the store row, to keep it in step with the task file: task=%s", task)
            return
    elif not _store_authoritative(ctx):
        _log.warning("healed surfaces not written to the task file: task=%s: no path", task)
        _log.warning("healed surfaces not written to the store row, to keep it in step with the task file: task=%s", task)
        return
    if row is None:
        return
    try:
        store_queue.upsert(
            ctx.store.conn,
            {"initiative": ctx.initiative_id, "task_id": task, "surfaces": list(surfaces), "extra": {**extra, "lint": list(lint)}},
        )
    except Exception as exc:
        _log.warning("healed surfaces not written to the store row: task=%s: %s: %s", task, type(exc).__name__, exc)


def _quarantine_row(ctx: _Ctx, phase: str, task: str, *, approved_is_open: bool = False) -> None:
    """Under work_state store, move the task's row to quarantined, so the chair stops relaunching it. Errors are logged, never raised.

    The files work state writes nothing here, and neither does a stale leader. Same store write as the move to approved: `_store_first`.
    """
    if not _store_authoritative(ctx):
        return
    try:
        stale = _fenced(ctx)  # callers in `_run_phase` reach here long after the phase-start fence
        if stale is not None:
            _log.warning("quarantined state not written to the store row: task=%s: %s", task, stale)
            return
        from harness import store_read  # not module level: see `_mirror_read`

        row = next((r for r in store_read.work_items(ctx.store.conn, ctx.initiative_id) if r["task_id"] == task), None)
        current = None if row is None else str(row["state"])
        if row_should_quarantine(ctx.work_state, current, approved_is_open=approved_is_open):
            _store_first(ctx, {"id": task, "phase": phase, "state": current}, "quarantined")
    except Exception as exc:
        _log.warning("quarantined state not written to the store row: task=%s: %s: %s", task, type(exc).__name__, exc)


def _mirror_write(ctx: _Ctx, item: Mapping[str, Any] | None, state: str) -> None:
    """Upsert one task's work_items row after an arm wrote `state` to its file. Errors are logged, never raised.

    Under work_state store the row was already written by `_store_first`, before the file, so nothing is upserted.
    """
    if ctx.store is None or item is None or _store_authoritative(ctx):
        return
    try:
        _upsert_row(ctx.store, work_mirror.item_row(ctx.initiative_id, {**item, "state": state}, _now(), _mirror_by(ctx)))
    except Exception as exc:
        _log.warning("work_items mirror (write) failed for %s: %s: %s", item.get("id"), type(exc).__name__, exc)


def _fenced(ctx: _Ctx) -> str | None:
    """None when a leader-only write may proceed, else the `stale epoch` reason. No epoch, no fence."""
    if ctx.epoch is None or ctx.store is None:
        return None
    conn, name, epoch = ctx.store.conn, ctx.lease_name or LEASE_NAME, ctx.epoch

    def check() -> str | None:
        if assert_epoch(conn, name, epoch, _now()):
            return None
        row = conn.query_one(f"SELECT epoch FROM leases WHERE name = {conn.dialect.placeholder}", (name,))
        held = "none" if row is None else row[0]
        return f"stale epoch: this driver holds epoch {epoch}, lease '{name}' is at epoch {held} or has expired"

    return ctx.retry.write(check, _END_WRITE_WINDOW_S)


def _is_stale_epoch(reason: str) -> bool:
    """True for the reason `_fenced` returns when the driver's lease is lost."""
    return reason.startswith("stale epoch")


# Only a store error is retried in the pause wait; a bug there still raises with its traceback.
_STORE_ERRORS: tuple[type[Exception], ...] = (sqlite3.Error,)
try:  # the postgres driver is an optional extra
    import psycopg

    _STORE_ERRORS = (*_STORE_ERRORS, psycopg.Error)
except ImportError:  # pragma: no cover - depends on what is installed
    pass

_PAUSE_LEASE_TTL = 120  # seconds; matches the CLI's own `_LEASE_TTL` (harness/cli.py) heartbeat renew.


def _wait_while_paused(
    conn: Any,
    run_id: str,
    lease_name: str,
    epoch: int,
    now: Callable[[], str],
    ttl: int,
    sleep: Callable[[float], None] = time.sleep,
    poll_seconds: float = 10,
    attempts: int = 3,
    retry_wait: float = 2.0,
) -> None:
    """Block while paused; a lost lease or a store down for `attempts` tries ends the run as SIGTERM does (143)."""

    def retried(call: Callable[[], Any]) -> Any:
        error: Exception | None = None
        for attempt in range(max(1, attempts)):
            if attempt:
                sleep(retry_wait)
            try:
                return call()
            except _STORE_ERRORS as exc:
                error = exc
        _log.warning("pause wait: store error after %d attempts: %s: %s", attempts, type(error).__name__, error)
        raise SystemExit(143) from error

    while retried(lambda: is_paused(conn, run_id)):
        # heartbeat says why: paused, not stuck
        if not retried(lambda: renew(conn, lease_name, run_id, epoch, now(), ttl, status="paused")):
            _log.warning("pause wait: lease '%s' epoch %s lost; ending the run", lease_name, epoch)
            raise SystemExit(143)
        sleep(poll_seconds)
    # always: clears a prior "paused" on resume too. A False return is left to the caller's fence re-check.
    retried(lambda: renew(conn, lease_name, run_id, epoch, now(), ttl, status=None))


def _clear_pause_if_held(
    store: Store, run_id: str, lease_name: str, epoch: int | None, retry: _StoreRetry | None = None
) -> None:
    """Clear the run's pause flag so a rerun does not stop at once; skipped when another holder owns the run."""

    def clear() -> None:
        if epoch is None or assert_epoch(store.conn, lease_name, epoch, _now()):
            clear_paused(store.conn, run_id)
        else:
            _log.warning("pause flag kept for %s: lease '%s' epoch %s is no longer held", run_id, lease_name, epoch)

    try:
        (retry or _StoreRetry()).write(clear, _END_WRITE_WINDOW_S)
    except _STORE_ERRORS as exc:  # a store failure must not mask the run's real exit
        _log.warning("pause flag clear failed for %s: %s: %s", run_id, type(exc).__name__, exc)


def _awaits_land(phase: Mapping[str, Any]) -> bool:
    """A phase waiting on a land."""
    return phase.get("status") == "awaiting_land"


def build_outcome(result: Mapping[str, Any]) -> dict[str, Any]:
    """The run's outcome from its epic result: totals, phase-grain quarantines, phases waiting on a parent.

    A waiting phase is a blocked record with `parents` entries: a parent did not complete, so it built nothing.
    """
    blocked = [
        {"phase": q["phase"], "reason": q["reason"]}
        for q in result.get("quarantined") or []
        if q.get("grain") == "phase"
    ]
    blocked_ids = {b["phase"] for b in blocked}
    waiting = [
        p["phase"]
        for p in result.get("phases") or []
        if p.get("status") == "blocked" and len(p.get("parents") or []) >= 1 and p["phase"] not in blocked_ids
    ]
    awaiting_land = any(_awaits_land(p) for p in result.get("phases") or [])
    interrupted = [
        {"phase": p["phase"], "reason": p.get("reason")}
        for p in result.get("phases") or []
        if p.get("status") == "interrupted"
    ]
    return {
        "totals": dict(result.get("totals") or {}),
        "blocked": blocked,
        "waiting": waiting,
        **({"interrupted": interrupted} if interrupted else {}),
        **({"blocked_kind": "awaiting_land"} if awaiting_land else {}),
    }


def _unlanded(
    tasks: Sequence[Mapping[str, Any]], run_id: str, repo: Path | str
) -> list[tuple[Mapping[str, Any], str]]:
    """Tasks approved but not landed, each with the `cox runs land` command a human runs for it.

    `outcome == "landed"` only ever meant "merged into the phase stack, not quarantined", never a claim that
    `cox runs land` ran, so such a task whose item still reads `approved` is unlanded too. `land` subsumes
    `recover` and never needs a phase branch resolved, so it is the command named here.
    """
    approved = [t for t in tasks if t.get("outcome") == "approved_not_landed"]
    merged = [t for t in tasks if t.get("outcome") == "landed" and t.get("state") == "approved"]
    return [(t, f"cox runs land {run_id} --repo {repo} --task {t['id']} --apply") for t in approved + merged]


def _integration_rows(record: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    """The check rows under `key`, shaped like `checks` rows; none when the key is absent, empty or not a list."""
    rows = record.get(key)
    return [r for r in rows if isinstance(r, Mapping)] if isinstance(rows, list) else []


def _integration_counts(tasks: Sequence[Mapping[str, Any]], phases: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Integration totals kept apart from the unit `tests_run`; a task or level with no integration rows counts in none."""
    task_rows = [_integration_rows(t, "integration") for t in tasks]
    level_rows = [_integration_rows(p, "level_integration") for p in phases]
    return {
        "integration_tasks_passed": sum(1 for rows in task_rows if rows and all(r.get("passed") for r in rows)),
        "integration_tasks_failed": sum(1 for rows in task_rows if rows and not all(r.get("passed") for r in rows)),
        "integration_levels_passed": sum(1 for rows in level_rows if rows and all(r.get("passed") for r in rows)),
        "integration_levels_failed": sum(1 for rows in level_rows if rows and not all(r.get("passed") for r in rows)),
        **{
            f"integration_rows_{k}": sum((r.get("counts") or {}).get(k, 0) for rows in task_rows for r in rows)
            for k in ("passed", "failed", "error")
        },
    }


def _build_result(
    *,
    run_id: str,
    date: str,
    initiative_id: str,
    phases: Sequence[Mapping[str, Any]],
    tasks: Sequence[Mapping[str, Any]],
    quarantined: Sequence[Mapping[str, Any]],
    proposals: Sequence[Mapping[str, Any]],
    stacks_rebased: int,
    paused_until: str | None,
    repo: Path | str,
    land_at: str | None = None,
    waves: Sequence[Sequence[str]] = (),
    lane: Mapping[str, Any] | None = None,
    lane_databases: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The epic result from the lists as they stand: whole at a normal end, partial when a phase raised."""
    unlanded = _unlanded(tasks, run_id, repo)
    return {
        "run_id": run_id,
        "date": date,
        "initiative": initiative_id,
        **({"land_at": land_at} if land_at else {}),
        "phases": phases,
        "waves": [list(w) for w in waves],
        "widest_wave": widest_wave(waves),
        "lane_bound": dict(lane or {}),
        **({"lane_databases": lane_databases} if lane_databases is not None else {}),
        "tasks": tasks,
        "quarantined": quarantined,
        "proposals": proposals,
        **({"paused_until": paused_until, "exit_code": EXIT_PAUSED} if paused_until else {}),
        **(
            {"exit_code": EXIT_AWAITING_LAND}
            if not paused_until and any(_awaits_land(p) for p in phases)
            else {}
        ),
        "exit_summary": (
            [f"paused: account session limit, resets {paused_until}"] if paused_until else []
        ) + [
            f"approved but not landed: {t['id']} — {command}" for t, command in unlanded
        ],
        "totals": {
            "phases_complete": sum(1 for p in phases if p["status"] == "complete"),
            "phases_partial": sum(1 for p in phases if p["status"] == "partial"),
            "phases_awaiting_land": sum(1 for p in phases if _awaits_land(p)),
            "phases_blocked": sum(1 for p in phases if p["status"] == "blocked"),
            "tasks_quarantined": sum(1 for q in quarantined if q.get("grain") == "task"),
            **_integration_counts(tasks, phases),
            "approved_not_landed": len(unlanded),
            "stacks_rebased": stacks_rebased,
        },
    }


def _record_run_exit(
    store: Store, run_id: str, status: str, retry: _StoreRetry, result: Mapping[str, Any] | None = None
) -> None:
    """Stamp the run's end, then store its outcome, each retried for the full exit window. A failure is logged and never changes the run's exit."""
    ended_at = _now()
    try:
        retry.write(lambda: store.finish_run(run_id, ended_at, status), _EXIT_WINDOW_S)
    except Exception as exc:
        _log.warning("run exit for %s not recorded in the store: %s: %s", run_id, type(exc).__name__, exc)
    if result is None:
        return
    try:
        retry.write(lambda: store.record_outcome(run_id, build_outcome(result)), _EXIT_WINDOW_S)
    except Exception as exc:
        _log.warning("run outcome for %s not recorded in the store: %s: %s", run_id, type(exc).__name__, exc)


def _publish_run_exit(run_id: str, initiative_id: str, status: str) -> None:
    """Publish `run_exited` once, after the exit is recorded. A failure is logged and never changes the run's exit."""
    try:
        published = publish_run_exited(run_id, initiative_id, status)
    except Exception as exc:
        _log.warning("run_exited for %s not published: %s: %s", run_id, type(exc).__name__, exc)
        return
    if not published:
        _log.warning("run_exited for %s not published: the courier was unreachable", run_id)


def _wait_if_paused(ctx: _Ctx) -> None:
    """`_wait_while_paused` bound to this run's own lease; a no-op without a fenced store."""
    if ctx.epoch is None or ctx.store is None:
        return
    _wait_while_paused(
        ctx.store.conn, ctx.run_id, ctx.lease_name or LEASE_NAME, ctx.epoch, _now, _PAUSE_LEASE_TTL, sleep=time.sleep,
    )


def _task_record_mirror(
    store: Store | None, result: Mapping[str, Any], run_id: str, phase: str, task: str, ts: str
) -> tuple[str, str, str, dict[str, Any], str] | None:
    """Arguments for `Store.record_task_record`, or None with no store to mirror into.

    The record is the JSON round trip the file gets, so the row equals the saved file.
    """
    if store is None:
        return None
    return (run_id, phase, task, json.loads(json.dumps(dict(result), default=str)), ts)


def _save_result(ctx: _Ctx, result: Mapping[str, Any], *, phase: str, task: str) -> None:
    """Save the result file, then mirror it into the store. The file stays authoritative.

    A mirror failure is a warning, never a failed run. The file write is outside the try.
    """
    save_result(result, runs_dir=ctx.runs_dir, run_id=ctx.run_id, phase=phase, task=task)
    try:
        args = _task_record_mirror(ctx.store, result, ctx.run_id, phase, task, _now())
        store = ctx.store

        def mirror() -> None:
            if _fenced(ctx) is None:
                store.record_task_record(*args)

        if args is not None and store is not None:
            ctx.retry.write(mirror, _END_WRITE_WINDOW_S)
    except Exception as exc:
        _log.warning("task record for %s/%s/%s not mirrored into the store: %r", ctx.run_id, phase, task, exc)


def _record_phase(ctx: _Ctx, record: Mapping[str, Any]) -> None:
    if ctx.store is not None and _fenced(ctx) is None:
        row = {**record, "run_id": f"{ctx.run_id}:{record['phase']}", "ts": _now(), "principal": PRINCIPAL}
        store = ctx.store
        ctx.retry.write(lambda: store.record_phase(row, epoch=ctx.epoch), _END_WRITE_WINDOW_S)


def _record_tasks(
    ctx: _Ctx, phase: str, task_records: Sequence[Mapping[str, Any]], quarantined: Sequence[Mapping[str, Any]]
) -> None:
    """One row per task at its final state: the store keeps the first row for a (run, task)."""
    if ctx.store is None or _fenced(ctx) is not None:
        return
    states = {str(q["id"]): "quarantined" for q in quarantined if q.get("grain") == "task"}
    states.update({str(t["id"]): str(t.get("state") or t["status"]) for t in task_records})
    ts = _now()
    store = ctx.store
    for task, state in sorted(states.items()):
        # One retry per row, so a lost connection never replays rows that already landed.
        ctx.retry.write(
            lambda task=task, state=state: store.record_task(ctx.run_id, phase, task, state, ts, epoch=ctx.epoch),
            _END_WRITE_WINDOW_S,
        )


def _refuse_stale(state: _Execution, *, phase: str, subject: str, slot: str, reason: str) -> None:
    """Quarantine what a stale leader tried to write. Not an attempt, so never through `_quarantine_task`."""
    task_grain = bool(subject) and slot != "rebase"
    entry = {
        "id": subject if task_grain else phase,
        "phase": phase,
        "grain": "task" if task_grain else "phase",
        "reason": reason,
        "kind": "no_work",
    }
    if entry not in state.quarantined:
        state.quarantined.append(entry)


def _frontmatter_block(body: str) -> str | None:
    """The text between the first pair of `---` fences, or None without one."""
    lines = body.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[: index + 1])
    return None


def _ticket_amend_ramp(old_body: str, new_body: str) -> str:
    """`ramp` for a `ticket_amend` proposal, per docs/design/triage.md §2.

    Purely additive — only appended or repeated lines, frontmatter untouched —
    rides the kind's streak (`"eligible"`); any removed line, or any change to
    the frontmatter block, forces `"gated"` whatever the streak. `difflib`
    decides; no model call.
    """
    if _frontmatter_block(old_body) != _frontmatter_block(new_body):
        return "gated"
    removed = any(line.startswith("- ") for line in difflib.ndiff(old_body.splitlines(), new_body.splitlines()))
    return "gated" if removed else "eligible"


def _amended_body(old_body: str, action: str) -> str:
    """`old_body`, a blank line, then `action`, with trailing whitespace trimmed."""
    return f"{old_body}\n\n{action}".rstrip()


def _is_append_only(old_body: str, new_body: str) -> bool:
    """True only when `new_body` is `old_body` byte for byte plus non-blank text after it."""
    return new_body.startswith(old_body) and bool(new_body[len(old_body):].strip())


def _read_ticket(item: Mapping[str, Any]) -> tuple[bytes, dict[str, Any]] | None:
    """The item's work file as raw bytes and parsed item; None when it has no file or the file cannot be read."""
    if not item.get("path"):
        return None
    try:
        return Path(str(item["path"])).read_bytes(), dict(workstore.read_item(str(item["path"])))
    except (OSError, ValueError, workstore.WorkStoreError):
        return None


def _kept_append_only(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    """True when `after` is `before` with text appended to the body and every other field unchanged."""
    old, new = str(before.get("body") or ""), str(after.get("body") or "")
    return (
        _is_append_only(old, new)
        and _ticket_amend_ramp(old, new) == "eligible"
        and {**before, "body": ""} == {**after, "body": ""}
    )


def _apply_amend_directly(
    ctx: _Ctx,
    proposal: dict[str, Any],
    item: Mapping[str, Any],
    *,
    old_body: str,
    new_body: str,
    phase: str,
    quarantined: list[dict[str, Any]],
    by_id: Mapping[str, dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None]:
    """Apply an append-only `ticket_amend` outside the gate, then judge the file the arm wrote, not the body predicted.

    `(rebuilt item, None)` when the file holds the old body with text appended. `(None, reason)` when the apply failed
    or wrote something else; the file is then restored to the bytes read before. `(None, None)` when the gate decides:
    the ramp is not eligible, the check is false, the cartridge names no model arm, or there is no readable ticket file.
    """
    before = _read_ticket(item)
    arm = apply_arm_for("ticket_amend", ctx.cartridge)
    if (
        proposal.get("ramp") != "eligible"
        or not _is_append_only(old_body, new_body)
        or arm in (None, "pr", "shell", "workstore")
        or before is None
        or str(before[1].get("body") or "").strip() != old_body.strip()
    ):
        return None, None
    applied, detail = _execute(
        ctx, proposal, slot="other", subject=str(item["id"]), phase=phase, state=_Execution({}, {}, {}, quarantined),
        by_id=by_id,
    )
    after = _read_ticket(item)
    if applied and after is not None and _kept_append_only(before[1], after[1]):
        return {**item, "body": str(after[1].get("body") or "")}, None
    restored = ""
    if after is not None and after[0] != before[0]:
        try:
            Path(str(item["path"])).write_bytes(before[0])
            restored = "; the ticket file was restored"
        except OSError:
            restored = "; the ticket file could not be restored"
    why = detail if not applied else "the ticket file does not hold the old body with text appended"
    return None, f"{why}{restored}"


def _newest_attempt_text(attempts: Sequence[Mapping[str, Any]]) -> str:
    """One line naming the newest attempt by `ts` (ties keep list order); the reason is cut to 120 characters."""
    newest = sorted(attempts, key=lambda a: str(a.get("ts") or ""))[-1:]
    if not newest:
        return "newest attempt: none recorded"
    a = newest[0]
    reason = " ".join(str(a.get("reason") or "").split())[:120]
    return f"newest attempt: kind {a.get('kind') or 'unrecorded'}, cause {a.get('cause') or 'unrecorded'}, reason {reason}"


INTERRUPTED_KIND = "interrupted"  # a task the lost lease stopped mid-run; never an attempt


def _build_attempts(attempts: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The attempts that count toward the cap: `infra`, `rescue_failed` and `interrupted` are not builds."""
    not_a_build = {"infra", rescue_select.RESCUE_KIND, INTERRUPTED_KIND}
    return [a for a in attempts if a.get("kind") not in not_a_build]


def _attempt_cap_reason(attempts: Sequence[Mapping[str, Any]]) -> str:
    """The refusal's reason, naming every earlier run so a person has the history."""
    history = "; ".join(f"{a.get('run')}: {a.get('reason')}" for a in attempts)
    return (
        f"attempt cap: {len(attempts)} earlier run(s) quarantined this task — {history}. "
        f"{_newest_attempt_text(attempts)}. "
        "Refusing a third run; a person decides."
    )


def _attempt_cap_for(attempts: Sequence[Mapping[str, Any]]) -> int:
    """1 when the newest attempt by `ts` (ties keep list order) was caused by the ticket or the code, else `ATTEMPT_CAP`."""
    newest = sorted(attempts, key=lambda a: str(a.get("ts") or ""))[-1:]
    return 1 if any(a.get("cause") in ("ticket", "code") for a in newest) else ATTEMPT_CAP


def _ready_view(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`items`, with a merged-but-unlanded parent presented as `done` for `needs`.

    `ready_tasks` satisfies a `needs` entry only against a literal `done`, and
    nothing in a run ever writes that — `cox runs land` does. Without this, a
    dependent phase's task would never become ready until a human landed the
    parent, even though the parent's code is already sitting on the phase
    branch stack the dependent builds from. Only a task that actually merged
    (`item["merged"]`) is presented this way; an escalated or conflicted
    parent stays `approved`, so its dependent still waits, unchanged. The
    view is read-only and local to this call — `items` itself, and every
    other read anywhere else, still shows `approved`.
    """
    return [
        {**item, "state": "done"} if item.get("state") == "approved" and item.get("merged") else item
        for item in items
    ]


def _merge_missing(ctx: _Ctx, phase: str, item: dict[str, Any]) -> str | None:
    """Why an approved, merged task of this phase cannot be resumed from the store, or None when its commit is on the phase branch.

    Only the store carries `merged_commit`, so a task merged under files is never checked. A task whose row is now done,
    landed by another machine, is adopted into `item` and is not missing.
    """
    commit = item.get("merged_commit")
    if not (_store_authoritative(ctx) and item.get("phase") == phase and item.get("state") == "approved" and commit):
        return None
    branch = ctx.phase_branch(phase)
    if base_holds(ctx.repo, str(commit), branch):
        return None
    (fresh,) = _store_fresh([item], _read_store_rows(ctx))
    if fresh["state"] == "done":
        with ctx.items_lock:
            item.update({k: fresh[k] for k in ("state", "merged", "merged_commit")})
        return None
    return f"merged commit {commit} is not on {branch}; {item['id']} was approved and merged by an earlier run and is not rebuilt"


def _quarantine_missing_merges(
    ctx: _Ctx, phase: str, items: Sequence[dict[str, Any]], quarantined: list[dict[str, Any]]
) -> None:
    """Quarantine each merged task whose commit is gone, one by one. No other item, row or file is touched."""
    with ctx.items_lock:
        by_id = {str(i["id"]): i for i in items}
        snapshot = list(items)
    for item in snapshot:
        why = _merge_missing(ctx, phase, item)
        if why is not None:
            quarantined.append(
                _quarantine_task(
                    ctx, by_id, phase=phase, task=str(item["id"]), reason=why, kind="infra",
                    cause=("harness", "merged commit missing"), approved_is_open=True,
                )
            )  # fmt: skip
            with ctx.items_lock:
                item["state"], item["merged"] = "quarantined", False


def _stale_base_missing(ctx: _Ctx, phase: str, item: Mapping[str, Any]) -> tuple[str, ...]:
    """Ticket files absent from the level worktree's head; empty when its tracked paths cannot be read."""
    ok, listing = _git("-C", str(ctx.phase_worktree(phase)), "ls-tree", "-r", "--name-only", "HEAD")
    if not ok:
        return ()
    named = ticket_paths([str(s) for s in item.get("surfaces") or []], str(item.get("body") or ""))
    return missing_paths(named, listing.splitlines())


def _invoke_failure_kind(failure: str) -> Literal["infra", "no_work"]:
    """`infra` for a transient OAuth refresh collision, which spends no attempt; `no_work` otherwise."""
    lowered = failure.lower()
    return "infra" if any(marker in lowered for marker in _TRANSIENT_AUTH) else "no_work"


def _buildable(
    items: list[dict[str, Any]],
    phase: str,
    foreign: Mapping[str, Any],
    only: frozenset[str] | None = None,
    land_at: str = DEFAULT_LAND_AT,
) -> list[dict[str, Any]]:
    """The phase's ready tasks with met needs, a dropped one excluded. A need on another initiative reads `foreign`.

    With `only`, the ready set is narrowed to those ids.
    """
    return [
        item
        for item in workstore.ready_tasks(_ready_view(items), phase=phase, foreign=foreign, land_at=land_at)
        if item.get("state") != "dropped" and (only is None or str(item["id"]) in only)
    ]


def _earlier_merged(items: Sequence[Mapping[str, Any]], phase: str, in_run: Collection[str]) -> list[Mapping[str, Any]]:
    """The phase's approved items outside this run, in item order, with or without a stored handoff."""
    return [
        item
        for item in items
        if item.get("phase") == phase and item.get("state") == "approved" and str(item["id"]) not in in_run
    ]


def _close_runner(runner: Any) -> Exception | None:
    """Close the shared runner's threads once, after every phase. The failure is returned, for the caller to raise last."""
    close = getattr(runner, "close", None)
    if not callable(close):
        return None
    try:
        close()
    except Exception as exc:
        return exc
    return None


class _PhaseRunner:
    """One phase's view of the shared runner: its own repo_dir, repo_digest and check_commands, nothing assigned to the shared one.

    Every other attribute and call forwards to the shared runner, and its methods run with this view as `self`,
    so the three values read per phase while counters and flags stay shared. A runner that delegates through
    `_inner` gets a view of the inner runner too, and public names go there, as `_DelegatingFastPath` does.
    A name the shared runner lacks stays absent, as the duck-typed `hasattr` checks expect.
    """

    def __init__(
        self,
        shared: Any,
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
        view = None if inner is None else _PhaseRunner(inner, repo_dir, repo_digest, check_commands, gate_check_commands)
        own = {} if inner is not None else {n: v for n, v in values.items() if hasattr(shared, n)}
        object.__setattr__(self, "_shared", shared)
        object.__setattr__(self, "_own", own)
        object.__setattr__(self, "_view", view)

    @property
    def __class__(self) -> type:  # so `super()` and `isinstance` inside the shared runner's methods accept this view
        return type(self._shared)

    def __getattr__(self, name: str) -> Any:
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

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._own:
            self._own[name] = value
        elif self._view is not None and not name.startswith("_"):
            setattr(self._view, name, value)
        else:
            setattr(self._shared, name, value)


def _base_expected(
    ctx: _Ctx, phase: str, parents: Sequence[str], items: Sequence[dict[str, Any]], runnable: Sequence[Mapping[str, Any]]
) -> tuple[Expected, ...]:
    """Approved, merged tasks the phase base must hold. A task done in the store is landed and is never checked."""
    rows = _read_store_rows(ctx)
    with ctx.items_lock:
        fresh = _store_fresh(items, rows)
        wanted = expected_merges(fresh, phase, [str(t["id"]) for t in runnable], parents)
    state = {str(i["id"]): i.get("state") for i in fresh}
    return tuple(e for e in wanted if state.get(e.task) != "done")


def _base_gaps(worktree: Path, expected: Sequence[Expected]) -> tuple[Expected, ...]:
    return gaps(expected, {e.task: base_holds(worktree, e.commit) for e in expected})


def _run_phase(
    ctx: _Ctx,
    *,
    initiative: Mapping[str, Any],
    phase: str,
    parents: tuple[str, ...],
    items: list[dict[str, Any]],
    only: frozenset[str] | None = None,
) -> dict[str, Any]:
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
        stacked = bool(parents) or _needs_other_phase(items, phase)
    pr_ref = ctx.pr_branch_ref() if stacked and ctx.land_at == "ticket" else None
    base_ref = pr_ref or (ctx.phase_branch(parent) if parent else ctx.default_ref)
    quarantined: list[dict[str, Any]] = []
    record: dict[str, Any] = {
        "phase": phase,
        "parent": parent,
        **({"assembled_from": list(parents)} if len(parents) > 1 and not pr_ref else {}),
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
    stale_start = _fenced(ctx)
    if stale_start:
        return _stale_phase(record, stale_start)

    # A branch under a parent is cut from the default ref and holds only the commits its tasks need.
    with ctx.items_lock:
        from_pr = pr_ref is not None
        needed = _required_refs(items, phase, parents, ctx.phase_branch, base_ref, from_pr=from_pr)
        cut_from = _required_refs(items, phase, parents, ctx.phase_branch, base_ref, cut=True, from_pr=from_pr)
    # A lone parent that is settled is the stack itself: cut from it, with no merge commit.
    cut_ref = base_ref if not parents or cut_from == [base_ref] else ctx.default_ref

    def open_failed(detail: str, verb: str) -> dict[str, Any]:
        record["status"] = "blocked"
        record["reason"] = _start_failure(detail, verb)
        if not detail.startswith(ASSEMBLY_FAILED):
            record["phase_failed_to_start"] = record["reason"]
        if detail.startswith(ASSEMBLY_FAILED) and only:
            # A conflict between needed commits holds the starting tasks only; the phase's other tasks stay as they were.
            with ctx.items_lock:
                by_id = {str(i["id"]): i for i in items}
            quarantined.extend(
                _quarantine_task(
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
    fetch, required, loose = _phase_branches_to_fetch(ctx.initiative_id, branch, parent_branches, needed, cut_from)
    if (stopped := _fetch_or_stop(ctx, record, fetch, required, loose, parent_branches)) is not None:
        return stopped

    opened = _open_phase_worktree(ctx, phase, cut_ref, parents, cut_from)
    ok, detail, reused = opened
    record["cleared_worktrees"] = list(getattr(opened, "cleared", ()))
    if not ok:
        return open_failed(detail, "created")
    record["reused_branch"] = reused
    _note_push_failure(record, _publish_branch(ctx, branch))

    # Decided before a single task builds: a reused branch missing a commit it
    # needs is either recreated (nothing of its own to lose) or blocked (something is).
    head_moved = reused and _needs_missing(ctx, phase, needed)
    with ctx.items_lock:
        task_states = {str(i["id"]): str(i.get("state")) for i in items}
    cherry = _phase_branch_cherry(ctx, phase, pr_ref) if head_moved else []
    unlanded_kind = classify_unlanded(cherry, task_states)
    action = branch_action(reused, head_moved, unlanded_kind not in ("landed", "rebuild"))

    foreign = initiative.get("foreign") or {}
    with ctx.items_lock:
        buildable_now = len(_buildable(items, phase, foreign, only, ctx.land_at))
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
        _git("-C", str(ctx.phase_worktree(phase)), "checkout", "--detach", "-q")
        return record

    if action == "block" and unlanded_kind == "awaiting_land":
        # Their lands are in flight: nothing to quarantine and nothing to rebase. Wait.
        record["status"] = "awaiting_land"
        record["awaiting_tasks"] = waiting_tasks(cherry)
        record["reason"] = (
            f"phase branch {branch} is behind {base_ref}; its unlanded commits belong to approved "
            "tasks whose lands are still in flight"
        )
        _git("-C", str(ctx.phase_worktree(phase)), "checkout", "--detach", "-q")
        return record

    if action == "block" and unlanded_kind == "rebase":
        # Some commits already landed (done), the rest are approved: replay the branch onto the new base.
        rebased_ok, rebase_detail = _rebase(ctx, phase, base_ref)
        if not rebased_ok:
            record["status"] = "blocked"
            record["reason"] = f"rebase conflict: {rebase_detail}"
            quarantined.append({"id": phase, "phase": phase, "grain": "phase", "reason": record["reason"]})
            _git("-C", str(ctx.phase_worktree(phase)), "checkout", "--detach", "-q")
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
        _git("-C", str(ctx.phase_worktree(phase)), "checkout", "--detach", "-q")
        return record

    def recreate() -> dict[str, Any] | None:
        """Cut the phase branch again from its base; the record to return when the leader is stale or the reopen fails."""
        stale_recreate = _fenced(ctx)
        if stale_recreate:
            return _stale_phase(record, stale_recreate)
        _git("-C", str(ctx.phase_worktree(phase)), "checkout", "--detach", "-q")
        with registry_lock():
            _git("-C", str(ctx.repo), "worktree", "remove", "--force", str(ctx.phase_worktree(phase)))
        _git("-C", str(ctx.repo), "branch", "-D", branch)
        reopened = _open_phase_worktree(ctx, phase, cut_ref, parents, cut_from)
        ok, detail, reused = reopened
        record["cleared_worktrees"] = [*record["cleared_worktrees"], *getattr(reopened, "cleared", ())]
        if not ok:
            return open_failed(detail, "recreated")
        record["reused_branch"] = reused
        _note_push_failure(record, _publish_branch(ctx, branch))
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

    # An approved task not yet recorded as merged has its land in flight: the branch is left as it is until that settles.
    with ctx.items_lock:
        land_in_flight = unlanded_kind == "awaiting_land" and any(
            i.get("phase") == phase and i.get("state") == "approved" and not i.get("merged") for i in items
        )
    if land_in_flight and ctx.land_at == "ticket" and record.get("reused_branch"):
        record["refresh_skipped"] = f"{branch} keeps its place: an approved task's land is still in flight"
        print(f"  {record['refresh_skipped']}")
    elif (early := _refresh_level(ctx, record, phase, parent, recreate)) is not None:
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
        runner=_PhaseRunner(
            ctx.runner,
            worktree,
            build_digest(worktree) or None,
            [str(c.get("cmd")) for c in ctx.checks if c.get("cmd")],
            gate_commands(ctx.cartridge.get("landing_areas") or {}),
        ),
    )

    # A task approved and merged in an earlier run is not rebuilt. Its commit must be on this phase's branch,
    # and a task whose commit is gone is quarantined alone: no other task's row or state is touched.
    _quarantine_missing_merges(ctx, phase, items, quarantined)
    with ctx.items_lock:
        handoffs = {str(i["id"]): i["handoff"] for i in items if i.get("handoff")}

    # A dropped task is terminal like a done one: it never gets rebuilt or
    # re-reviewed, and never blocks the phase behind it.
    with ctx.items_lock:
        all_ready = _buildable(items, phase, foreign, only, ctx.land_at)

    # A third run of the same task is refused outright rather than tried
    # again — quarantined here, plainly, never through `_quarantine_task`,
    # because a refusal is not an attempt and must not grow the count it is
    # enforcing.
    # An `infra` attempt is the apply arm's own failure, not the task's, so it
    # must not grow the count the cap enforces. Nor does a `rescue_failed` one:
    # a rescue checks work already built and is not a build.
    attempts_by_id = {str(item["id"]): _build_attempts(item.get("attempts") or []) for item in all_ready}
    # A rewritten ticket starts a fresh count. Triage still gets the whole history for its repeat guard.
    # An item with no `body` cannot be matched, so all its attempts count.
    counted_by_id = {
        str(item["id"]): (
            workstore.attempts_on_current_body({**item, "attempts": attempts_by_id[str(item["id"])]})
            if "body" in item
            else attempts_by_id[str(item["id"])]
        )
        for item in all_ready
    }
    caused_by_id = {
        task_id: with_stored_causes({"attempts": counted}, _stored_causes(ctx, task_id))["attempts"] if counted else counted
        for task_id, counted in counted_by_id.items()
    }
    cap_by_id = {task_id: _attempt_cap_for(caused) for task_id, caused in caused_by_id.items()}
    # A capped task is launched into `triage` (docs/design/triage.md §1) rather
    # than quarantined outright: the graph classifies the attempt record and
    # emits by class, and only a class `triage` could not turn into a write —
    # or a repeated `(class, diagnosis)` it refuses to clear twice — still ends
    # up quarantined here, plainly, never through `_quarantine_task`.
    triage_batch: list[dict[str, Any]] = []
    amended: dict[str, dict[str, Any]] = {}  # task id -> the item rebuilt on its append-only amended body
    capped_ids = sorted(task_id for task_id, attempts in counted_by_id.items() if len(attempts) >= cap_by_id[task_id])
    if capped_ids:
        all_ready_by_id = {str(item["id"]): item for item in all_ready}
        _wait_if_paused(ctx)
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
                    },
                )
                for task_id in capped_ids
            ],
            specs=ctx.specs,
            runner=ctx.runner,
            run_id=f"{ctx.run_id}:{phase}",
            max_parallel=ctx.max_parallel,
        )
        for failure in triage_failures:
            task_id = failure.split(":", 1)[0]
            reason = f"{_attempt_cap_reason(attempts_by_id[task_id])} triage itself failed: {failure}"
            quarantined.append({"id": task_id, "phase": phase, "grain": "task", "reason": reason, "kind": "no_work"})
            _quarantine_row(ctx, phase, task_id)
            print(
                f"  attempt cap: {task_id} refused; triage could not run ({failure}); "
                f"{_newest_attempt_text(attempts_by_id[task_id])}"
            )

        for result in triaged:
            task_id = str(result.get("run_id", "")).rsplit(":", 1)[-1]
            attempts = attempts_by_id[task_id]
            if result.get("escalated"):
                reason = (
                    f"{_attempt_cap_reason(attempts)} triage repeats a prior "
                    f"({result.get('class')!r}, {result.get('diagnosis')!r}) diagnosis; escalating to a person."
                )
                quarantined.append({"id": task_id, "phase": phase, "grain": "task", "reason": reason, "kind": "no_work"})
                _quarantine_row(ctx, phase, task_id)
                print(
                    f"  attempt cap: {task_id} launched triage, which escalated a repeated diagnosis; "
                    f"{_newest_attempt_text(attempts)}"
                )
                continue

            emitted = dict(result.get("emit") or {})
            if "kind" not in emitted:
                reason = (
                    f"{_attempt_cap_reason(attempts)} triage classified this as "
                    f"{result.get('class')!r} with no direct write; a person decides."
                )
                quarantined.append({"id": task_id, "phase": phase, "grain": "task", "reason": reason, "kind": "no_work"})
                _quarantine_row(ctx, phase, task_id)
                print(
                    f"  attempt cap: {task_id} launched triage -> {result.get('class')} (no proposal); "
                    f"{_newest_attempt_text(attempts)}"
                )
                continue

            if emitted["kind"] == "ticket_amend":
                old_body = str(all_ready_by_id[task_id].get("body") or "")
                new_body = _amended_body(old_body, str(emitted.get("suggested_action", "")))
                emitted["ramp"] = _ticket_amend_ramp(old_body, new_body)
                rebuilt, failure = _apply_amend_directly(
                    ctx, emitted, all_ready_by_id[task_id], old_body=old_body, new_body=new_body, phase=phase,
                    quarantined=quarantined, by_id={str(item["id"]): item for item in items},
                )
                if rebuilt is not None:
                    amended[task_id] = rebuilt
                    print(
                        f"  attempt cap: {task_id} launched triage -> append-only ticket_amend applied; rebuilding; "
                        f"{_newest_attempt_text(attempts)}"
                    )
                    continue
                if failure is not None:
                    if not any(q.get("id") == task_id and q.get("grain") == "task" for q in quarantined):
                        reason = f"{_attempt_cap_reason(attempts)} triage proposed an append-only amendment; applying it failed: {failure}"
                        quarantined.append({"id": task_id, "phase": phase, "grain": "task", "reason": reason, "kind": "no_work"})
                        _quarantine_row(ctx, phase, task_id)
                    print(
                        f"  attempt cap: {task_id} append-only ticket_amend not applied ({failure}); "
                        f"{_newest_attempt_text(attempts)}"
                    )
                    continue
            triage_batch.append(emitted)
            print(
                f"  attempt cap: {task_id} launched triage -> {result.get('class')} ({emitted['kind']}); "
                f"{_newest_attempt_text(attempts)}"
            )
    all_ready = [amended.get(str(item["id"]), item) for item in all_ready]
    counted_by_id = {**counted_by_id, **{task_id: [] for task_id in amended}}
    ready = [item for item in all_ready if len(counted_by_id[str(item["id"])]) < cap_by_id[str(item["id"])]]

    by_id = {str(item["id"]): amended.get(str(item["id"]), item) for item in items}
    if hasattr(ctx.runner, "verify_by_task"):
        ctx.runner.verify_by_task = {**ctx.runner.verify_by_task, **{t: _verify_of(by_id, t) for t in by_id}}
    results: list[dict[str, Any]] = []

    # Resume: a task whose earlier run already produced an approved patch is
    # reused at no cost. Applying and checking it below is unchanged, so a
    # patch that no longer fits the phase branch quarantines on its own merits.
    reused: list[dict[str, Any]] = []
    to_run = list(ready)
    if ctx.resume_from:
        to_run = []
        for task in ready:
            saved = load_result(ctx.runs_dir, ctx.resume_from, phase, str(task["id"]))
            if reusable(saved):
                reused.append(_without_cause(saved))
                print(f"  reused {task['id']} from {ctx.resume_from} (approved patch, no model call)")
            else:
                to_run.append(task)
    record["reused_tasks"] = [str(r.get("ticket")) for r in reused]

    # A task whose item asks for a build budget above the cartridge's cap
    # never reaches the model at all — refused outright, plainly, the same
    # way the attempt cap above is: never through `_quarantine_task`, because
    # this is not an attempt either and must not grow the count that gates it.
    cap = (ctx.cartridge.get("policy") or {}).get("build_budget_usd_max")
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
        _quarantine_row(ctx, phase, task_id)
        print(f"  build budget: {task_id} refused (budget_usd {task['budget_usd']} > cap {cap})")
    over_budget_ids = {str(task["id"]) for task in over_budget}
    within_budget = [task for task in to_run if str(task["id"]) not in over_budget_ids]

    # A surface naming a file the task creates but lacking `(new)` is healed, not
    # refused: the marker and a lint note are added, written back so a relaunch
    # does not heal again, and the build goes on with the healed task. Only a
    # malformed surface (absolute, outside the repository, a glob) is refused
    # plainly, like the budget refusal above: not an attempt, so never through
    # `_quarantine_task`, and no model call is made.
    # Judged against the fetched default branch, not the checkout, which may be stale.
    surface_paths = frozenset[str]()
    if any(task.get("surfaces") for task in within_budget):
        listed = _default_branch_paths(ctx)
        surface_paths = listed if listed is not None else _working_tree_paths(ctx.repo)
    refused_ids: set[str] = set()
    healed_by_id: dict[str, dict[str, Any]] = {}
    heal_notes: dict[str, list[str]] = {}
    ticket_corrections: dict[str, list[str]] = {}
    # The ticket check reads file contents at the commit the listing was taken from. It never refuses or
    # quarantines: when the commit cannot be read, the check raises, is logged, and the task builds unchanged.
    commit = _default_branch_commit(ctx) if surface_paths else ""
    defined, search = ticket_lookups(str(ctx.repo), commit, _git_stdout)
    for task in (t for t in within_budget if t.get("surfaces")):
        task_id = str(task["id"])
        bad = malformed_surfaces([str(s) for s in task["surfaces"]])
        if bad:
            refused_ids.add(task_id)
            quarantined.append({
                "id": task_id, "phase": phase, "grain": "task", "kind": "no_work", "reason": _surface_refusal_reason(bad),
            })
            _record_task_cause(ctx, phase, task_id, "ticket", "rule: surfaces not in the repository")
            _quarantine_row(ctx, phase, task_id)
            print(f"  surfaces: {task_id} refused (malformed: {', '.join(bad)})")
            continue
        # The ticket check runs first: a bare filename must be resolved before heal_new_markers can mark it `(new)`.
        # Prose notes are moved out of surfaces first, so the check does not read them as filenames.
        prepared = notes_out_of_surfaces(task)
        try:
            checked, corrections = check_ticket(prepared, surface_paths, defined, search)
        except Exception as exc:
            _log.warning("ticket check failed, task builds unchanged: task=%s: %s: %s", task_id, type(exc).__name__, exc)
            checked, corrections = prepared, ()
        if corrections:
            ticket_corrections[task_id] = list(corrections)
        healed = _without_correction_lint(_heal_surfaces(checked, surface_paths), task)
        if healed == dict(task):
            if corrections:
                print(f"  ticket check: {task_id} {len(corrections)} correction(s) for the builder")
            continue
        healed_by_id[task_id] = healed
        known = {str(n) for n in task.get("lint") or []}
        heal_notes[task_id] = [str(n) for n in healed.get("lint") or [] if str(n) not in known]
        _write_healed_surfaces(
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
                           .get("build") or {}).get("patch")
        for task in runnable
        if task.get("attempts")
    }

    # The phase worktree is shared by the phase's tasks, so the base is checked once, before any build. A base
    # missing merged work is rebuilt by merging that work in; one that cannot be rebuilt stops the phase. No
    # task is quarantined and no attempt is recorded: the tasks stay ready for the next launch.
    if runnable:
        worktree = ctx.phase_worktree(phase)
        expected = _base_expected(ctx, phase, parents, items, runnable)
        missing = _base_gaps(worktree, expected)
        if missing:
            fetch_origin(ctx.repo)
            merged_ok, merge_detail = merge_in(worktree, [g.commit for g in missing])
            remaining = _base_gaps(worktree, expected)
            if remaining or not merged_ok:
                stopped = remaining or missing
                record["status"] = "blocked"
                record["reason"] = stale_base_stop(stopped)
                quarantined.append({
                    "id": stopped[0].task, "phase": phase, "grain": "phase", "kind": "stale_base",
                    "reason": record["reason"],
                })
                _git("-C", str(worktree), "checkout", "--detach", "-q")
                print(f"  base check: {phase} stopped ({record['reason']}; {merge_detail or 'commit not found'})")
                return record
            record["base_rebuilt"] = sorted({g.task for g in missing})
            print(f"  base check: {phase} base rebuilt with {', '.join(record['base_rebuilt'])}")

    budget_sources: dict[str, Any] = {}
    if runnable:
        _wait_if_paused(ctx)
        invocations = [
            _lifecycle_invocation(
                ctx,
                task,
                body=plan_context(
                    {
                        **task,
                        "body": _carry_forward(
                            task.get("body") or "",
                            list(task.get("attempts") or []),
                            patches_for_attempt.get(str(task["id"])),
                            limit=PATCH_FOR_VALIDATION_CHARS,
                        )
                        + "".join(f"\n\nSurface note: {note}" for note in heal_notes.get(str(task["id"]), []))
                        + "".join(f"\n\nCorrection note: {note}" for note in ticket_corrections.get(str(task["id"]), [])),
                    },
                    handoffs,
                ),
                fix_attempts=ctx.fix_attempts,
            )
            for task in runnable
        ]
        budget_sources = {inv.id: inv.args["build_budget_source"] for inv in invocations}
        results, _, failures = invoke_graphs(
            invocations,
            specs=ctx.specs,
            runner=ctx.runner,
            run_id=f"{ctx.run_id}:{phase}",
            max_parallel=ctx.max_parallel,
        )
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
            missing = _stale_base_missing(ctx, phase, by_id[failed]) if empty else ()
            quarantined.append(
                _quarantine_task(
                    ctx, by_id, phase=phase, task=failed,
                    reason=stale_base_reason(failed, missing) if missing else failure,
                    kind="stale_base" if missing else _invoke_failure_kind(failure),
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
        _save_result(ctx, result, phase=phase, task=str(result.get("ticket")))

    built: dict[str, dict[str, Any]] = {}
    surviving: list[str] = []
    escalated: set[str] = set()

    for result in sorted(results, key=lambda r: str(r.get("ticket"))):
        task = str(result.get("ticket"))

        # A non-build node raised mid-round: review, adversary, arbitrate,
        # validate or handoff never reached a verdict, so nothing here was
        # judged and `_unapproved` has nothing to read. The patch a later
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
            quarantined.append(_quarantine_task(ctx, by_id, phase=phase, task=task, reason=reason, kind="infra"))
            continue

        # The loop's refusal is the answer, and it is free. Applying a patch the
        # reviewers rejected costs a worktree, a check run and both validators
        # before anything says no, and what finally says no is a validator
        # reading a stale verdict rather than the loop that actually decided.
        refused = _unapproved(result)
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
                _quarantine_task(ctx, by_id, phase=phase, task=task, reason=refused, kind="refused", result=result)
            )
            continue

        build = _build_task(ctx, phase=phase, task=task, result=result, verify=_verify_of(by_id, task))
        final, build = _refix(ctx, by_id, phase=phase, task=task, result=result, build=build)
        build["result"] = final
        built[task] = build
        if "checks" in build:
            # The result file was saved before the checks ran. `runs detail` reads that file, so the
            # per-check results (excerpt and log name) go into it under `change_facts`, as the
            # task record below carries them.
            facts = {**(final.get("change_facts") or {}), "checks": build["checks"]}
            _save_result(ctx, {**final, "change_facts": facts}, phase=phase, task=task)

        # Evidence first, escalation second — the same order `cli.py` uses, and
        # for the same reason: the gate should see the tests' opinion of a
        # governance change too.
        for item in final.get("proposals") or []:
            if item.get("kind") in _DRAFT_KINDS:
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

        # `_execute` sees only the batch item, so the verdicts ride on it.
        arbitration = final.get("arbitration")
        for item in build["proposals"]:
            if item.get("escalated_from") == "draft_pr_create":
                item["review_verdict"] = str((final.get("review") or {}).get("verdict") or "")
                item["arbitration_verdict"] = str(
                    arbitration.get("verdict") if isinstance(arbitration, Mapping) else arbitration or ""
                )

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
                **({"test_database": build["test_database"]} if "test_database" in build else {}),
                "lint_fixed": build.get("lint_fixed", False),
                "lint_fix_checks": build.get("lint_fix_checks", []),
                "build_budget_source": budget_sources.get(task),
                **({"refix": build["refix"]} if build.get("refix") else {}),
            }
        )
        if build.get("quarantine"):
            reason = build["quarantine"]
            # A harness fault is not an attempt: the build was never actually
            # tried, so recording one here would burn the same two-strike cap
            # a real failure does. Mirrors the attempt-cap refusal above,
            # which quarantines without ever calling `_quarantine_task`.
            if is_harness_fault(reason):
                quarantined.append({"id": task, "phase": phase, "grain": "task", "reason": reason, "kind": "no_work"})
                _quarantine_row(ctx, phase, task)
            else:
                # This branch is only reached for a task the fix loop already
                # approved — `_unapproved` above quarantines anything else as
                # `refused` before a worktree is even opened. A check still
                # failing here, after the lint_fix step had its chance, is a
                # non-functional finding on an approved patch, not grounds to
                # discard it: `unverified` keeps it, same as `validate_chunk`.
                detail = check_feedback(build.get("checks") or []) or None
                quarantined.append(
                    _quarantine_task(
                        ctx, by_id, phase=phase, task=task, reason=reason, kind="unverified", detail=detail
                    )
                )
            continue
        surviving.append(task)

    # ── validation, between the fan-out and the merges ───────────────────────
    chunk_by_task: dict[str, dict[str, Any]] = {}
    verdict: dict[str, Any] | None = None
    validated = "validate_phase" in ctx.bound

    # A phase is judged once, when it is finished: a run carrying only part of it
    # would be scored against a goal its siblings have not yet built toward.
    # A task an earlier run approved counts as finished, and joins the validator's input.
    # One store read: a task another machine landed since this run read its items counts as finished.
    store_rows = _read_store_rows(ctx)
    with ctx.items_lock:
        fresh_items = _store_fresh(items, store_rows)
    earlier = _earlier_merged(fresh_items, phase, {str(r.get("ticket")) for r in results})
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
            "phase": {"id": phase, "goal": _phase_goal(initiative, phase)},
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
                        "reader": _read_evidence_file,
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
            gaps = ", ".join(chunk.get("gaps") or []) or str(chunk.get("reasoning", ""))
            quarantined.append(
                _quarantine_task(
                    ctx, by_id, phase=phase, task=task,
                    reason=f"validate_chunk unsatisfied: {gaps}", kind="unverified",
                )
            )
            for task_record in record["task_records"]:
                if task_record["id"] == task:
                    task_record["status"] = "quarantined"
                    task_record["quarantine"] = f"validate_chunk unsatisfied: {gaps}"

    # ── the phase's one gate batch, in task-id order ─────────────────────────
    batch, slots = _build_batch(
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
    auto, gated = _policy_split(ctx, batch)
    # At ticket, with no `assume` given, nobody is asked and nobody decides: the batch is auto-cleared. Auto-cleared
    # work writes no gate diff and no ledger row, so unattended runs add nothing to a kind's graduation streak.
    if land_gate_level(ctx.land_at) == "ticket" and ctx.assume is None:
        auto, gated = [*auto, *gated], []
    auto_ids = {id(item) for item in auto}
    decisions, human_minutes = gate(gated, assume=ctx.assume) if gated else ([], 0.0)
    decided = {id(item): (decision, edited) for item, decision, edited in decisions}

    state = _Execution(
        landed={}, merged={}, moved={}, quarantined=quarantined, escalated=frozenset(escalated),
        builds={task: (built[task]["result"].get("build") or {}) for task in surviving},
    )  # fmt: skip
    diffs: list[dict[str, Any]] = []

    for item in batch:
        slot, subject = slots.get(id(item), ("other", ""))
        if id(item) in auto_ids:
            applied, _ = _execute(ctx, item, slot=slot, subject=subject, phase=phase, state=state, by_id=by_id)
            # Auto-cleared: NO gate diff and NO ledger row. Autonomy is spent by
            # acting; a row here would let a kind ratchet itself up on its own
            # say-so, which is the self-report the ledger exists to disbelieve.
            record["rebased"] = record["rebased"] or (applied and slot == "rebase")
            continue

        decision, edited = decided.get(id(item), ("refused", False))
        applied = False
        if decision == "approved":
            applied, _ = _execute(ctx, item, slot=slot, subject=subject, phase=phase, state=state, by_id=by_id)
            record["rebased"] = record["rebased"] or (applied and slot == "rebase")
        # Built here rather than by `gate.apply_decisions`, which cannot know
        # about a branch this driver created: `applied` is what actually
        # happened, so an approved merge that conflicted records `skipped`.
        diffs.append(gate_diff(item, decision, applied=applied, edited=edited))

    for warning in state.push_warnings:
        _note_push_failure(record, warning)

    # At `task` each task merged by this run gets its own PR; at `level` and `ticket` none does. A failed PR is a
    # note on the task, not a quarantine: the merge into the phase branch already happened.
    if opens_pr(ctx.land_at, "task"):
        held = {q["id"] for q in state.quarantined if q.get("grain") == "task"}
        # A restart also covers the tasks a first run merged and approved, whose PR may never have opened.
        stored_merged = {str(i["id"]) for i in items if i.get("phase") == phase and i.get("state") == "approved" and i.get("merged")}
        for task in sorted(({t for t in surviving if state.merged.get(t)} | stored_merged) - held):
            ok, detail = _open_grain_pr(
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

    # `_execute` can quarantine a task after its merge already succeeded — the
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
            str((verdicts.get("arbitration") if isinstance(verdicts.get("arbitration"), Mapping) else {}).get("verdict") or "") or None,
            task_record.get("quarantine"),
            # Merged AND quarantined only happens when `_execute`'s own
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

    _record_tasks(ctx, phase, record["task_records"], quarantined)

    record["status"], reason = _phase_status(
        verdict,
        validated=validated,
        ready=ready,
        quarantined=quarantined,
        items=_store_fresh(items, store_rows),
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
        _run_level_checks(ctx, phase, record)
    if record["status"] == "complete":
        _run_level_integration(ctx, phase, str(record["base"]), record)

    # At `level` a finished level gets one PR. A restart over a merged level finds the PR it already has, or opens
    # the one a failed first attempt never made. A level stacked on one parent is based on that parent's PR branch when
    # that PR exists, so it shows only its own commits; otherwise it falls back to the default branch.
    if opens_pr(ctx.land_at, "level") and record["status"] == "complete":
        default_base = ctx.default_ref.removeprefix("origin/")
        parent_head = f"review/{ctx.initiative_id}/{parent}" if parent else None
        parent_known, parent_pr = _existing_pr(ctx, parent_head) if parent_head else (False, "")
        ok, detail = _open_grain_pr(
            ctx,
            grain="level",
            name=phase,
            local=branch,
            title=f"{ctx.initiative_id}: {phase}",
            base=parent_head if parent_head and parent_known and parent_pr else default_base,
        )
        record["review_pr" if ok else "review_pr_error"] = detail

    # docs/design/landing-model.md §5: one style_pass over the assembled phase
    # diff, run only once validate_phase itself has said the goal is met — a
    # trim is not a second opinion on completeness, it runs after that
    # question is already settled.
    if record["status"] == "complete" and verdict is not None and verdict.get("goal_met") and "style_pass" in ctx.bound:
        stale_trim = _fenced(ctx)
        outcome = f"refused: {stale_trim}" if stale_trim else _trim_phase(ctx, phase)
        if outcome is not None:
            record["trim"] = f"trim: {outcome}"

    # Release the phase branch. Git refuses to check one branch out in two
    # worktrees, and re-entrancy — a later run building on the branch this one
    # left — is a stated requirement, so the worktree keeps its files and gives
    # the branch back. Nothing here deletes work: the branches are the artifact.
    _git("-C", str(ctx.phase_worktree(phase)), "checkout", "--detach", "-q")

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
    stale = _fenced(ctx)
    if stale is not None:
        return _stale_phase(record, stale)
    append_ledger(manifest, ledger_path=ctx.ledger_path)  # the host file is written once, never inside a retry
    if ctx.store is not None:
        store = ctx.store
        # Ledger rows are built in `core.manifest.append_ledger`; the store copies this phase's rows back by run id.
        for row in ledger.read(ctx.ledger_path):
            if row.get("run_id") == manifest["run_id"]:
                ctx.retry.write(lambda row=row: store.record_ledger(row, epoch=ctx.epoch), _END_WRITE_WINDOW_S)
        ctx.retry.write(
            lambda: store.record_gate_decisions(ctx.run_id, phase, diffs, epoch=ctx.epoch), _END_WRITE_WINDOW_S
        )
    record["manifest"] = f"{ctx.run_id}:{phase}"
    record["manifest_record"] = manifest
    return record


def _stale_phase(record: dict[str, Any], reason: str) -> dict[str, Any]:
    """Stop the phase on a fence, so it neither reads complete nor unblocks its dependents.

    A lost lease leaves the phase `interrupted`. Its in-flight tasks, and the task entries `_quarantine_task`
    returned once fenced, move from `quarantined` to `interrupted`. Merged and otherwise quarantined tasks stay.
    """
    record["reason"] = reason
    if not _is_stale_epoch(reason):
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


def _fenced_task_entry(entry: Mapping[str, Any]) -> bool:
    """A task quarantine entry that `_quarantine_task` returned because the lease was already lost."""
    return entry.get("grain") == "task" and _is_stale_epoch(str(entry.get("reason")))


def _missing_branch_phase(record: dict[str, Any], branch: str, cause: str) -> dict[str, Any]:
    """Block the phase on a branch it needs and cannot find. Nothing is quarantined: the run stops, the work is untouched."""
    record["status"] = "blocked"
    record["reason"] = cause
    record["missing_branch"] = branch
    return record


def _collected_ids(worktree: Path) -> set[str] | None:
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


def _trim_phase(ctx: _Ctx, phase: str) -> str | None:
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
    ok, diff = _git("-C", str(ctx.repo), "diff", f"{ctx.default_ref}...{branch}")
    if not ok or not diff.strip():
        return None
    # The only direct `ctx.runner.run` call this module owns. No `task=` here:
    # `style_pass` runs once over the whole PHASE diff — every task's work
    # combined — so there is no single task id to stamp, and its ledger row
    # is `role: "style_pass"`, which `_trace_evidence`'s `role == "build"`
    # filter excludes regardless of `task_id` (see the test naming this call).
    result = ctx.runner.run(
        role="style_pass",
        tier="standard",
        schema=_STYLE_PASS_SCHEMA,
        wait_if_paused=lambda: _wait_if_paused(ctx),
        prompt=f"{_STYLE_PASS_PROMPT}\n\nPhase: {phase}\n\nPhase diff:\n{diff}",
        context=list(ctx.cartridge.get("context") or []),
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
            before = _collected_ids(before_tree) if ok else None

            ok, _ = create_worktree(ctx.repo, after_tree, branch=after_branch, base=branch)
            applied = ok and apply_patch(patch, after_tree)[0]
            after = _collected_ids(after_tree) if applied else None

            if before is None or after is None:
                outcome = "refused (coverage floor)"
            else:
                checks_ok = all_passed(run_checks(after_tree, ctx.checks, env=_checks_env(ctx, {}))) if ctx.checks else True
                if not (coverage_floor_holds(before, after) and checks_ok):
                    outcome = "refused (coverage floor)"
        finally:
            remove_worktree(ctx.repo, before_tree)
            remove_worktree(ctx.repo, after_tree)
            _git("-C", str(ctx.repo), "branch", "-D", before_branch)
            _git("-C", str(ctx.repo), "branch", "-D", after_branch)
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
    ok, _ = _git(*_IDENTITY, "-C", str(worktree), "add", "-u")
    if ok:
        ok, _ = _git(*_IDENTITY, "-C", str(worktree), "commit", "-q", "-m", f"epic {ctx.run_id}: trim {phase}")
    if not ok:
        return "refused (coverage floor)"
    removed = sum(1 for line in patch.splitlines() if line.startswith("-") and not line.startswith("---"))
    return f"{removed} lines removed"


def _phase_status(
    verdict: Mapping[str, Any] | None,
    *,
    validated: bool,
    ready: Sequence[Mapping[str, Any]],
    quarantined: Sequence[Mapping[str, Any]],
    items: Sequence[Mapping[str, Any]],
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


def _finding_entries(result: Mapping[str, Any], chunk: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Every (text, file) a review, adversary or validate verdict wrote for one task.

    Only a review finding's `file` is structured: REVIEW_SCHEMA requires it on
    every entry (`graphs/delivery/lifecycle_propose.py`), so it is read straight
    off rather than guessed at. An adversary objection and a validate gap or
    reasoning string carry no such field — `("text", "")` — and are matched on
    their own prose in `_cited_surface` below.
    """
    review = (result.get("review") or {}).get("findings") or []
    adversary = (result.get("adversary") or {}).get("objections") or []
    entries = [(str(f.get("detail") or ""), str(f.get("file") or "")) for f in review]
    entries += [(f"{o.get('claim') or ''} {o.get('why_wrong') or ''}".strip(), "") for o in adversary]
    entries += [(str(g), "") for g in chunk.get("gaps") or []]
    entries.append((str(chunk.get("reasoning") or ""), ""))
    return entries


def _cited_surface(text: str, file: str, surfaces: Sequence[str]) -> str | None:
    """The one entry of `surfaces` that `file` names outright, or that `text` quotes."""
    if file and file in surfaces:
        return file
    return next((s for s in surfaces if s and s in text), None)


def _consolidation_pairs(
    built: Mapping[str, Mapping[str, Any]],
    surviving: Sequence[str],
    chunk_by_task: Mapping[str, Mapping[str, Any]],
    by_id: Mapping[str, Mapping[str, Any]],
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
        entries = _finding_entries(built[task]["result"], chunk_by_task.get(task) or {})
        for other in surviving:
            if other == task or frozenset({task, other}) in seen:
                continue
            surfaces = [str(s) for s in (by_id.get(other) or {}).get("surfaces") or []]
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


def state_move_apply(path: str | None, state: str) -> dict[str, Any]:
    """The `apply` payload the code arm reads; empty when no path is known, so the gate falls back to the model arm."""
    return {"apply": {"path": str(path), "state": state}} if path else {}


def item_apply(path: str | None, item: Mapping[str, Any]) -> dict[str, Any]:
    """The `apply` payload for item_create and item_update; empty when no path is known."""
    return {"apply": {"path": str(path), "item": dict(item)}} if path else {}


def _build_batch(
    ctx: _Ctx,
    *,
    phase: str,
    surviving: Sequence[str],
    built: Mapping[str, Mapping[str, Any]],
    escalated: set[str],
    chunk_by_task: Mapping[str, Mapping[str, Any]],
    rebase: Mapping[str, Any] | None,
    by_id: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[int, tuple[str, str]]]:
    """Everything this phase asks for, in task-id order, plus what each slot means.

    The slot map is by identity rather than by target string, because the same
    proposal objects travel through `split_by_policy` and `gate` unchanged and
    the driver has to know, at execution time, which branch a given proposal
    was about. Reparsing a target would be a second encoding of the same fact.
    """
    batch: list[dict[str, Any]] = []
    slots: dict[int, tuple[str, str]] = {}

    for task in surviving:
        for item in built[task]["proposals"]:
            batch.append(item)
            if item.get("kind") in _DRAFT_KINDS:
                slots[id(item)] = ("draft", task)

    for task in surviving:
        merge = proposal(
            ctx.cartridge,
            kind="merge_stack",
            target=f"{ctx.draft_branch(phase, task)} -> {ctx.phase_branch(phase)}",
            evidence=[
                *built[task]["evidence"],
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
                patch=str((built[task]["result"].get("build") or {}).get("patch") or ""),
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
                    "output": str((built[task]["result"].get("review") or {}).get("verdict")),
                },
                *built[task]["evidence"],
            ],
            rationale=f"{task} was built, checked and reviewed in this run",
            # `approved`, never `done`: the apply arm writes the state this names, and only
            # `cox runs land` writes `done`, after the merge (tools #153).
            suggested_action=f"mark {task} approved",
        )
        move = {**move, **state_move_apply((by_id.get(task) or {}).get("path"), "approved")}
        batch.append(move)
        slots[id(move)] = ("state_move", task)

    for pair in _consolidation_pairs(built, surviving, chunk_by_task, by_id):
        task, other, file, text = pair["task"], pair["other"], pair["file"], pair["text"]
        ids = " and ".join(sorted((task, other)))
        # Propose only, per conventions.md's propose-don't-write posture: no
        # slots entry, so `_execute`'s default ("other", "") slot never merges
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


@dataclass
class _Execution:
    """What actually happened, per task, as the batch executes."""

    landed: dict[str, bool]
    merged: dict[str, bool]
    moved: dict[str, bool]
    quarantined: list[dict[str, Any]]
    review_pr: dict[str, str] = field(default_factory=dict)
    escalated: frozenset[str] = frozenset()
    notes: dict[str, str] = field(default_factory=dict)
    draft_detail: dict[str, str] = field(default_factory=dict)
    push_warnings: list[str] = field(default_factory=list)  # origin pushes that failed; the branch stays local
    commits: dict[str, str] = field(default_factory=dict)  # task -> the commit its merge made on the phase branch
    builds: dict[str, Mapping[str, Any]] = field(default_factory=dict)  # task -> its build result, for the handoff
    handoffs: dict[str, dict[str, Any]] = field(default_factory=dict)  # task -> the handoff stored with its merge


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


def _unkept_work(ctx: _Ctx, phase: str, subject: str, state: _Execution) -> str | None:
    """Read what happened to the task's draft and merge, and report why its work is not kept, if it is not."""
    escalated = subject in state.escalated
    branch = ctx.scratch_branch(subject) if escalated else ctx.phase_branch(phase)
    return kept_work_refusal(
        landed=_branch_exists(ctx, branch) if escalated else bool(state.landed.get(subject)),
        merged=state.merged.get(subject),
        escalated=escalated,
        ahead=_branch_ahead(ctx, branch),
    )


def draft_skipped_note(*, landed: bool, merged: bool | None, escalated: bool, branch_kept: bool, detail: str) -> str | None:
    """The note for an approved task whose draft did not land but whose scratch branch holds its work, else None.

    A merge that ran and failed is not this case: that task is quarantined by its merge. `detail` is why the draft is absent.
    """
    if escalated or landed or merged is not None or not branch_kept:
        return None
    return f"approved without a draft branch: {detail}. The work is on the task branch; `runs land` creates the PR."


def _draft_note(ctx: _Ctx, subject: str, state: _Execution) -> str | None:
    """Read the scratch branch and the draft step's outcome, and return the note when the work is kept without a draft."""
    scratch = ctx.scratch_branch(subject)
    return draft_skipped_note(
        landed=bool(state.landed.get(subject)),
        merged=state.merged.get(subject),
        escalated=subject in state.escalated,
        branch_kept=kept_work_refusal(
            landed=_branch_exists(ctx, scratch), merged=None, escalated=True, ahead=_branch_ahead(ctx, scratch)
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


def _holds(ctx: _Ctx, commit_branch: str | None, branch: str) -> bool:
    """True when `commit_branch`'s tip is an ancestor of `branch`. A missing branch on either side is False."""
    if not commit_branch:
        return False
    ok, _ = _git(
        "-C", str(ctx.repo), "merge-base", "--is-ancestor", f"refs/heads/{commit_branch}", f"refs/heads/{branch}"
    )
    return ok


def _gather_branch_facts(ctx: _Ctx, records: Sequence[Mapping[str, Any]]) -> dict[str, BranchFacts]:
    """Per task id, the git facts `end_of_run_refusal` judges. The only part of the audit that reads git."""
    facts: dict[str, BranchFacts] = {}
    for record in records:
        task = str(record["id"])
        merged = bool(record.get("merged"))
        draft = record.get("draft")
        branch = ctx.phase_branch(str(record["phase"])) if merged else ctx.scratch_branch(task)
        facts[task] = BranchFacts(
            branch=branch,
            draft=draft,
            exists=_branch_exists(ctx, branch),
            holds_merge=_holds(ctx, draft, branch) if merged else True,
            ahead=_branch_ahead(ctx, branch),
        )
    return facts


def _audit_approved_kept(
    ctx: _Ctx, by_id: Mapping[str, dict[str, Any]], tasks: Sequence[dict[str, Any]], quarantined: list[dict[str, Any]]
) -> None:
    """Quarantine each task this run moved to approved whose work is no longer on a branch. Never recovers it."""
    approved = [t for t in tasks if t.get("state") == "approved" and not t.get("quarantine")]
    facts = _gather_branch_facts(ctx, approved)
    for record in approved:
        why = end_of_run_refusal(facts[str(record["id"])], merged=bool(record.get("merged")))
        if why is None:
            continue
        # `no_work`, not `infra`: `infra` marks a patch as kept, and the branch that kept it is what went missing.
        entry = _quarantine_task(
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
        ok, detail = _rebase(ctx, phase, _rebase_base(item))
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
            dict(item), cartridge=ctx.cartridge, runner=ctx.runner, wait_if_paused=lambda: _wait_if_paused(ctx)
        )
    except LimitStop:
        raise  # the account's own limit, not this task's; never through `_quarantine_task`
    except RunnerError as exc:
        arm = apply_arm_for(item.get("kind"), ctx.cartridge)
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
        record_merge(ctx.store.conn, ctx.initiative_id, task, commit, _now(), _mirror_by(ctx))
        record_handoff(ctx.store.conn, ctx.initiative_id, task, handoff, _now(), _mirror_by(ctx))
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
        cartridge=ctx.cartridge,
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

    verified = rescue_checks.verify_patch(
        ctx.repo,
        ctx.default_ref,
        patch,
        ctx.checks,
        apply=apply,
        workdir=ctx.worktree_root / ctx.run_id,
        env=_checks_env(ctx, {}),
    )
    if not verified["passed"]:
        return _rescue_failed(ctx, item, phase=phase, reason=str(verified["reason"]), failed_checks=True)

    rows = verified["rows"]
    evidence = [*((saved or {}).get("evidence") or []), *rows]
    _save_result(
        ctx,
        {**_without_cause(saved or {}), "initiative": ctx.initiative_id, "phase": phase, "evidence": evidence},
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
        ctx, {**graph, "initiative": ctx.initiative_id, "phase": phase, "evidence": evidence}, phase=phase, task=task
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
