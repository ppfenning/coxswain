"""The phase lane: one claimed phase, its tasks built in parallel, each rolled up onto the phase branch as it passes.

A worker claims a phase (`store_phases.claim_next`) and starts this lane under the claim's epoch (`shell.py phase-lane
--lane-worker W --claim-epoch E`, through `cox route launch epic --lane-worker`). The lane:

1. checks it still holds the claim, and keeps checking on a thread of its own (`Fence`) on a connection of its own;
2. reads where the phase branch stands on origin (`lane_rollup.fetch`) and what it already holds (`lane_progress.read`
   of the `Cox-*` trailers): git is the authority for what is built, so a lane that claims after another finds the
   tasks already rolled up and builds only the rest; a roll-up whose step row was lost is written again;
3. joins that with the phase's last review round (`lane_progress.resume_plan`): with tasks missing it builds them; a
   phase with every task rolled up goes to the smoke (7), then waits for its review, which is L6's, so the lane ends
   there;
4. starts each task once everything it needs inside the phase has rolled up (`lane_plan.next_tasks`), up to its share
   of the machine's threads (`lane_plan.threads`), each in a worktree of its own cut from the phase tip at that moment;
5. per task: the lifecycle graph in build-only mode (plan, build, handoff; never a review, which is the phase's), then
   the cheap checks in the task's worktree (the cartridge's and `.agent-checks`' task checks, narrowed to what the
   change touches: unit tests, lint, the privacy check), with the task's own verification commands;
6. the roll-up, in order: the checks passed, their artifacts uploaded, the `checks` step row written; the phase's local
   lock taken; the change applied cleanly onto the phase tip (a conflict fails the task: resolving one is L5's) and
   committed with the trailers; the claim checked again; pushed; the claim checked once more; the `rollup` step row
   written with the commit. A push that does not land is reconciled, not taken for a lost claim
   (`lane_rollup.next_move`): the claim is checked and origin fetched; a commit of this task another lane landed is
   adopted (and any roll-up row it left out written), a branch that moved has the change re-applied onto it, a push
   that failed to arrive is tried again; past three tries only that task fails.
7. once every task has rolled up, the phase smoke (`_smoke`, `lane_smoke`): the `level` gate (the cartridge's
   `level_checks` and `.agent-checks`' `# level: phase` section) run once in the phase worktree at the tip, never in
   a task's, and one `smoke` step row for the round it precedes. A failed smoke writes no review and does not park the
   phase `built`: the worker sends it back to the queue with the failure (`phase_pick.lane_verdict`).

**What is fenced, and what is left.** Every step row is written only while the claim is held, by the store's clock
(`store_phases.put_steps_fenced`: one transaction, under a share lock on the claim row), so a lane that lost its claim
writes no row, ever. The lane stops itself, its agents and checks with it (their process groups), once its claim is
lost or its lease is within `phase_pick.LANE_MARGIN_S` (15 s) of lapsing: by then its worker has gone the 45 s it may
go without a heartbeat. A push follows a claim check that found at least that margin and is bounded to
`lane_rollup.REMOTE_TIMEOUT_S` (8 s), so a lane that keeps running cannot push once its lease has lapsed. What no check
can close is a lane frozen (stopped, swapped out, its machine suspended) between its last check and the moment git
sends the refs: it may wake after another lane holds the claim. Then the push is a plain fast-forward: if the new
holder pushed first it is refused; if not, it lands one commit, which is a whole roll-up of a task built on the phase
tip and carries its trailers, and the new holder adopts it (above) rather than building it twice. The stale lane
writes no row for it (the check after the push fails), and the bound on the push means a wake long after the check
kills a push still in flight. So the most a stale lane can do is land, once, a commit the phase would have had anyway.

On one machine the worker also stops any older lane of the phase before it starts a new one, and stops a lane whose
progress mark (`runs/<run>.lane-beat`, rewritten by the fence's thread after each good check) stops moving, so a lane
frozen under a live worker cannot hold its phase forever. The lane takes no `leases` row (the claim is its lease),
opens the store through `store_scoped.open_scoped` as the worker's scoped role, and never writes `work_items`, the
initiative branch or the landing record; the chair turns the roll-ups into approved tasks when the phase is approved.

The rules are pure (`lane_plan`, `lane_progress`, `lane_rollup.message`, and the helpers here marked pure); the rest is
the edge.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import signal
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, cast

from graphs._contract import ContractViolation
from harness import lane_plan, lane_progress, lane_rollup, lane_smoke, phase_pick, repo_cache, store_phases
from harness.artifacts import mark_missing, step_artifacts
from harness.check_gate import repo_gate_lock
from harness.checks import run_checks
from harness.epic_branches import LIFECYCLE, checks_env
from harness.epic_build import LANE_BASE_VAR, build_task, drop_lane, lifecycle_invocation, settle_pool, verify_of
from harness.epic_context import Ctx, git_in
from harness.handoff import plan_context
from harness.lane_workers import LANE_CAPACITY_VAR
from harness.store_dialect import Connection, JsonObject
from harness.store_steps import step_key, step_row, steps_of_result, with_checks
from harness.store_types import StepRow
from harness.worktree import clone_worktree, remove_worktree
from runner.protocol import LimitStop, RunnerError

__all__ = [
    "EXIT_BUILT",
    "EXIT_FAILED",
    "EXIT_LOST",
    "Fence",
    "LaneClaim",
    "TaskCtx",
    "base_candidates",
    "beat_path",
    "exit_code",
    "lane_url",
    "lanes_of",
    "missing_rollups",
    "phase_tasks",
    "run_lane",
    "smoke_due",
    "smoke_round",
    "task_dag",
    "terminate_groups",
    "threads_for",
    "write_beat",
]

EXIT_BUILT = 0  # every task rolled up, or nothing was left to build
EXIT_FAILED = 1  # a task failed, or the lane could not start
EXIT_LOST = 5  # the claim was lost: nothing more was written or pushed (3 and 4 are the epic's paused and awaiting-land)
WATCH_S = 2.0  # how often the fence reads the claim
_SKIPPED = frozenset({"dropped", "done"})


# -- pure ------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class LaneClaim:
    """The claim this lane runs under: whose it is and the epoch every write of it carries."""

    initiative: str
    phase: str
    worker: str
    epoch: int


def phase_tasks(items: Sequence[Mapping[str, Any]], phase: str) -> list[Mapping[str, Any]]:
    """Pure. The phase's tasks the lane builds, by id: every task of the phase not dropped or done."""
    return sorted((i for i in items if str(i.get("phase") or "") == phase and str(i.get("state") or "") not in _SKIPPED),
                  key=lambda i: str(i["id"]))


def task_dag(tasks: Sequence[Mapping[str, Any]]) -> dict[str, tuple[str, ...]]:
    """Pure. Each task's needs, as `lane_plan` reads them."""
    return {str(t["id"]): tuple(str(n) for n in t.get("needs") or []) for t in tasks}


def base_candidates(row_base: str, record_branch: str, initiative: str, fallback: str) -> list[str]:
    """Pure. The branches a phase branch is cut from, first found on origin wins: the queue row's `base_ref`, the
    initiative's branch from its landing record, `init/<initiative>`, then the launch's base (`origin/<target>`). The
    lane only reads them; the initiative branch is the lander's."""
    names = [b.removeprefix("origin/") for b in (row_base, record_branch, f"init/{initiative}", fallback) if b]
    return list(dict.fromkeys(names))


def lane_url(owner_url: str, role: str, password: str, worker_url: Callable[[str, str, str], str]) -> tuple[str, str]:
    """Pure. The store URL the lane writes through, and who it writes as: the worker's scoped role when the unit's
    environment names one with its password, else the URL it was given (a store with no worker role yet)."""
    if role and password and owner_url.startswith(("postgresql", "postgres")):
        return worker_url(owner_url, role, password), role
    return owner_url, "the profile's store user"


def exit_code(lost: bool, failed: Sequence[str], smoke_passed: bool = True) -> int:
    """Pure. The lane's exit: lost beats failed (a task, or the phase smoke) beats built."""
    return EXIT_LOST if lost else EXIT_FAILED if failed or not smoke_passed else EXIT_BUILT


def smoke_due(failed: Mapping[str, str], stopping: bool) -> bool:
    """Pure. Whether the lane runs the phase smoke: every task rolled up (none failed) and the lane is not stopping."""
    return not failed and not stopping


def smoke_round(nxt: lane_progress.Next) -> int:
    """Pure. The review round the smoke comes before: the round `resume_plan` gave a lane that goes to review, and
    round 1 for one that built the phase's tasks (it builds only before the first round)."""
    return nxt.round if nxt.action == "review" else 1


def missing_rollups(done: Mapping[str, str], stored: Sequence[Mapping[str, object]], phase: str) -> dict[str, str]:
    """Pure. The tasks git shows rolled up whose roll-up commit has no `rollup` step row in any run: a lane that died
    between its push and its row. The lane claiming next writes those rows."""
    have = {(str(r.get("task_id")), str(r.get("subject_sha"))) for r in stored if r.get("phase") == phase}
    return {task: sha for task, sha in sorted(done.items()) if (task, sha) not in have}


@dataclass(frozen=True)
class TaskCtx(Ctx):
    """The run's context for one task: its "phase branch" is the phase tip the task was cut from, so the epic's task
    build (`epic_build.build_task`) cuts the worktree there, diffs its checks against it, and names it to the lane's
    checks as their base (`COX_LANE_BASE`)."""

    base_ref: str = ""

    def phase_branch(self, phase: str) -> str:
        return self.base_ref

    @classmethod
    def of(cls, ctx: Ctx, base_ref: str) -> TaskCtx:
        return cls(**{f.name: getattr(ctx, f.name) for f in fields(Ctx)}, base_ref=base_ref)


# -- the edges -------------------------------------------------------------------------------------------------------

class Fence:
    """Edge. The claim as the lane sees it. A thread of its own reads the claim row every `WATCH_S` on a connection
    of its own; once the claim is gone, or its lease is within `LANE_MARGIN_S` of lapsing, `lost` is set and
    `on_lost` runs once (it stops the lane's agents and checks). Any part of the lane may also `lose` it, when a
    fenced write was refused or the branch moved under it. A read that fails counts as lost: the lane cannot show it
    still holds the claim, and stopping is the safe side."""

    def __init__(self, open_conn: Callable[[], Connection], claim: LaneClaim, log: Callable[[str], None],
                 on_lost: Callable[[], None], beat: Callable[[int], None] = lambda _n: None) -> None:
        self._open, self._claim, self._log, self._on_lost, self._beat = open_conn, claim, log, on_lost, beat
        self._conn: Connection | None = None
        self._lock = threading.Lock()
        self._halt = threading.Event()
        self.lost = threading.Event()
        self.why = ""
        self._thread: threading.Thread | None = None

    def check(self) -> bool:
        """Read the claim now; True while it is held with the margin left. Sets `lost` when it is not."""
        if self.lost.is_set():
            return False
        c = self._claim
        try:
            with self._lock:
                if self._conn is None:
                    self._conn = self._open()
                ok = store_phases.held(self._conn, c.initiative, c.phase, c.worker, c.epoch, phase_pick.LANE_MARGIN_S)
        except Exception as exc:
            self.lose(f"the claim could not be read: {type(exc).__name__}: {' '.join(str(exc).split())[:200]}")
            return False
        if not ok:
            self.lose(f"the claim at epoch {c.epoch} is no longer {c.worker}'s, or its lease is about to lapse")
        return ok

    def lose(self, why: str) -> None:
        with self._lock:
            if self.lost.is_set():
                return
            self.why = why
            self.lost.set()
        self._log(f"lane: stopping: {why}")
        self._on_lost()

    def start(self) -> None:
        """The watch thread: a claim check every `WATCH_S`, and after each good one the progress mark the worker
        watches (`beat`), so a lane that stops checking stops marking too."""

        def loop() -> None:
            count = 0
            while True:
                count += 1
                try:
                    self._beat(count)
                except OSError as exc:  # a mark that cannot be written ages, and the worker stops the lane: say why
                    self._log(f"lane: the progress mark could not be written: {exc}")
                if self._halt.wait(WATCH_S) or not self.check():
                    return

        self._thread = threading.Thread(target=loop, name="lane-fence", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._halt.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


@dataclass
class Lane:
    """Edge. One run of the lane: the run's context, the claim, the store connection its writes go through (under
    one lock, since the task threads share it), the fence, and the phase worktree the roll-ups are committed in."""

    ctx: Ctx
    claim: LaneClaim
    conn: Connection
    fence: Fence
    phase_worktree: Path
    branch: str
    base_sha: str
    items: Mapping[str, Mapping[str, Any]]
    log: Callable[[str], None]
    open_conn: Callable[[], Connection]
    store_lock: threading.Lock = field(default_factory=threading.Lock)
    # Uploads go through a connection of their own, so a slow bucket never holds the row writes a roll-up waits on.
    artifacts_lock: threading.Lock = field(default_factory=threading.Lock)
    artifacts_conn: Connection | None = None
    handoffs: dict[str, Mapping[str, Any]] = field(default_factory=dict[str, Mapping[str, Any]])
    halted: threading.Event = field(default_factory=threading.Event)
    # The shared checkout's context, when the lane rolls up in a clone of its own (`_own_clone`). Its git directory
    # holds the phase's local lock, still one file for every lane of the phase on this machine, and the tasks build in
    # it as before, so the warm pool and its synced venvs keep serving them.
    shared: Ctx | None = None

    def now(self) -> str:
        with self.store_lock:
            return store_phases.store_now(self.conn)

    def put(self, rows: Sequence[StepRow], result: Mapping[str, object] | None = None) -> bool:
        """Upload the rows' artifacts (never raises), then write the rows, fenced. False, and the fence lost, when the
        store refused them: the claim is gone. Nothing at all once the lane is stopping for a lost claim, even while
        the lease has not yet lapsed: the lane gave the claim up, so its rows are no longer its to write."""
        c = self.claim
        if self.fence.lost.is_set():
            return False
        listed = tuple(rows)
        if result is not None and rows and self.ctx.artifacts is not None:
            with self.artifacts_lock:
                try:
                    if self.artifacts_conn is None:
                        self.artifacts_conn = self.open_conn()
                    listed = step_artifacts(self.ctx.artifacts, self.artifacts_conn, rows, result,
                                            runs_dir=self.ctx.runs_dir, run_id=self.ctx.run_id, phase=c.phase,
                                            task=rows[0]["task_id"])
                except Exception as exc:  # the rows are still written, marked, as every failed upload's are
                    listed = tuple(mark_missing(r, f"{type(exc).__name__}: {exc}"[:500]) for r in rows)
        with self.store_lock:
            written = store_phases.put_steps_fenced(self.conn, listed, c.worker, c.epoch)
        if not written:
            self.fence.lose("a step row was refused: the claim is gone")
        return written

    def row(self, task: str, kind: str, ok: bool, verdict: str, result: JsonObject, started: str,
            sha: str = "") -> StepRow:
        c = self.claim
        return step_row(run_id=self.ctx.run_id, step=step_key("task", task, kind, 0), initiative=c.initiative,
                        phase=c.phase, task_id=task, kind=kind, round_=0, ok=ok, verdict=verdict, result=result,
                        finished_at="", started_at=started, subject_sha=sha, worker=c.worker, claim_epoch=c.epoch)

    def stopping(self) -> bool:
        return self.fence.lost.is_set() or self.halted.is_set()

    def close(self) -> None:
        with self.artifacts_lock:
            if self.artifacts_conn is not None:
                self.artifacts_conn.close()
                self.artifacts_conn = None


def _rows_of(lane: Lane, result: Mapping[str, object], task: str, started: str) -> tuple[StepRow, ...]:
    c = lane.claim
    rows = steps_of_result(result, run_id=lane.ctx.run_id, initiative=c.initiative, phase=c.phase, task=task, at="",
                           claim_epoch=c.epoch, worker=c.worker)
    return tuple(cast(StepRow, {**r, "started_at": started}) for r in rows)


def _build(lane: Lane, task: str, base: str) -> tuple[Mapping[str, object] | None, str, str, TaskCtx]:
    """Edge. The task's build-only lifecycle run, with its plan and build rows written: (result, why not, start)."""
    started = lane.now()
    tctx = TaskCtx.of(lane.shared or lane.ctx, base)
    shared, why = _share_base(lane, task, base)
    if not shared:
        lane.put([lane.row(task, "build", False, "failed", {"error": why[:500]}, started)])
        return None, why, started, tctx
    item = lane.items[task]
    invocation = lifecycle_invocation(tctx, item, body=plan_context(item, lane.handoffs), fix_attempts=0)
    args = {**invocation.args, "run_id": f"{lane.ctx.run_id}:{task}", "build_only": True}
    try:
        result = json.loads(json.dumps(lane.ctx.specs[LIFECYCLE].run(args, lane.ctx.runner), default=str))
    except LimitStop as exc:
        lane.halted.set()
        lane.put([lane.row(task, "build", False, "stopped", {"error": str(exc)[:500]}, started)])
        return None, f"the account's limit stopped the build: {exc}", started, tctx
    except (ContractViolation, RunnerError) as exc:
        lane.put([lane.row(task, "build", False, "failed", {"error": str(exc)[:500]}, started)])
        return None, f"the build failed: {exc}", started, tctx
    rows = _rows_of(lane, result, task, started)
    lane.put(rows, result)
    stopped = result.get("build_stopped")
    return (None if stopped else result), str(stopped or ""), started, tctx


def _share_base(lane: Lane, task: str, base: str) -> tuple[bool, str]:
    """Edge. Make the task's base, a commit of the lane's clone, a commit of the shared checkout the task builds in:
    a local fetch under a ref private to the run, which `drop_refs` removes at the end. Nothing to do without a clone."""
    if lane.shared is None:
        return True, ""
    ref = lane_rollup.private_ref(lane.ctx.run_id, f"base/{task}")
    ok, said = git_in("-C", str(lane.ctx.repo), "update-ref", ref, base)
    if ok:
        ok, said = git_in("-C", str(lane.shared.repo), "fetch", "--no-tags", "--quiet", "--no-write-fetch-head",
                          str(lane.ctx.repo), f"+{ref}:{ref}")
    return ok, "" if ok else f"the task's base {base[:12]} could not be brought into the shared checkout: {said[:300]}"


def _cleanup(lane: Lane, tctx: TaskCtx, task: str) -> None:
    """Edge. The task's database and scratch, then its worktree (a pooled one goes back with the pool) and branch."""
    phase = lane.claim.phase
    drop_lane(tctx, phase, task)
    if tctx.pool is None:
        remove_worktree(tctx.repo, tctx.task_worktree(phase, task))
        git_in("-C", str(tctx.repo), "branch", "-D", tctx.scratch_branch(task))


def _origin_progress(lane: Lane, tip: str) -> dict[str, str]:
    """Edge. The tasks origin's phase branch at `tip` holds, each with its commit; and a `rollup` row written, as
    recovered, for each of them another lane landed without one (`missing_rollups`)."""
    c = lane.claim
    ok, text = lane_rollup.log_since(lane.ctx.repo, lane.base_sha, tip)
    if not ok:
        return {}
    done = lane_progress.read(text, c.initiative, c.phase).done
    with lane.store_lock:
        stored = store_phases.rollup_rows(lane.conn, [c.initiative])
    recovered = missing_rollups(done, stored, c.phase)
    if recovered:
        start = lane.now()
        lane.put([lane.row(t, "rollup", True, "recovered", {"recovered": True}, start, sha)
                  for t, sha in recovered.items()])
    return done


def _roll_up(lane: Lane, task: str, base: str, tctx: TaskCtx, started: str) -> tuple[bool, str]:
    """Edge. The roll-up under the phase's local lock: apply, commit, check the claim, push, check it again, then the
    `rollup` row. A push that does not land goes to `lane_rollup.next_move`; only a lost claim stops the lane."""
    c = lane.claim
    ok, diff = lane_rollup.change(tctx.task_worktree(c.phase, task), base)
    if not ok:
        return False, f"the task's change could not be read: {diff[:300]}"
    text = lane_rollup.message(c.initiative, c.phase, task, lane.ctx.run_id, str(lane.items[task].get("title") or task))
    with lane_rollup.phase_lock((lane.shared or lane.ctx).repo, c.initiative, c.phase):
        attempt = 0
        while True:
            attempt += 1
            if lane.stopping():
                return False, "the lane is stopping"
            tip = lane_rollup.head(lane.phase_worktree)
            sha, why = lane_rollup.apply_and_commit(lane.phase_worktree, diff, text)
            if sha is None:
                lane.put([lane.row(task, "rollup", False, "conflict", {"reason": why[:500], "tip": tip}, started)])
                return False, why
            if not lane.fence.check():
                lane_rollup.reset_to(lane.phase_worktree, tip)
                return False, "the claim was lost before the push"
            outcome, said = lane_rollup.push(lane.phase_worktree, lane.branch)
            if outcome == "pushed":
                if not lane.fence.check():
                    return False, f"{sha[:12]} was pushed, but the claim was lost meanwhile: the next claim records it"
                lane.log(f"lane: {task} rolled up as {sha[:12]} on {lane.branch}")
                lane.put([lane.row(task, "rollup", True, "pushed", {"base": base, "parent": tip}, started, sha)])
                return True, sha
            held = lane.fence.check()
            origin, fetch_why = lane_rollup.fetch(lane.ctx.repo, lane.ctx.run_id, lane.branch) if held else (None, "")
            on_origin = _origin_progress(lane, origin) if origin else {}
            move = lane_rollup.next_move(held, origin, sha, tip, task in on_origin, attempt)
            lane.log(f"lane: {task}'s push of {sha[:12]} came back {outcome} ({' '.join(said.split())[:200]}"
                     f"{'; ' + fetch_why[:200] if fetch_why else ''}); next: {move}")
            if move == "done":
                lane.put([lane.row(task, "rollup", True, "pushed", {"base": base, "parent": tip}, started, sha)])
                return True, sha
            if move == "adopt":
                lane_rollup.reset_to(lane.phase_worktree, str(origin))
                return True, on_origin[task]
            if move == "reapply":
                lane_rollup.reset_to(lane.phase_worktree, str(origin))
                continue
            lane_rollup.reset_to(lane.phase_worktree, tip)
            if move == "retry":
                continue
            if move == "lost":
                return False, "the claim was lost after a push that did not land"
            return False, f"the push did not land in {attempt} tries: {' '.join(said.split())[:300]}"


def _task(lane: Lane, task: str, base: str) -> tuple[bool, str]:
    """Edge. One task, start to roll-up, on a thread of the lane's pool: (rolled up, the commit or why not)."""
    if lane.stopping():
        return False, "the lane is stopping"
    result, why, started, tctx = _build(lane, task, base)
    try:
        if result is None:
            return False, why
        phase = lane.claim.phase
        record = build_task(tctx, phase=phase, task=task, result=result, verify=verify_of(lane.items, task))
        checked = json.loads(json.dumps(with_checks(result, record.get("checks") or [], lane.ctx.run_id), default=str))
        if not lane.put(_rows_of(lane, checked, task, started), checked):
            return False, "the claim was lost"
        if quarantined := record.get("quarantine"):
            return False, str(quarantined)
        rolled, detail = _roll_up(lane, task, base, tctx, started)
        if rolled and isinstance(result.get("handoff"), Mapping):
            lane.handoffs[task] = cast(Mapping[str, Any], result["handoff"])
        return rolled, detail
    finally:
        _cleanup(lane, tctx, task)


SMOKE_SCRATCH = "@smoke"  # the smoke's scratch name: `@` keeps it apart from every task id (`epic_branches.scratch_dir`)


def _smoke(lane: Lane, tip: str, round_: int) -> bool:
    """Edge. The phase smoke at `tip` before review round `round_`, in the phase worktree (checked out at `tip`): the
    level gate run once under the repository's gate lock, or an earlier passing run of it on this host (the worker)
    at this tip reused (`lane_smoke.reusable`), then the `smoke` step row. Whether it passed; a lost claim writes no row and is False.

    The lane runs it once every task has rolled up; L6 runs it again after each revision, with that round's number."""
    c, ctx = lane.claim, lane.ctx
    checks = ctx.level_checks
    cmds = [str(check.get("cmd") or "") for check in checks]
    started = lane.now()
    with lane.store_lock:
        prior = lane_smoke.reusable(store_phases.smoke_rows(lane.conn, c.initiative, c.phase), tip, cmds, c.worker)
    results: list[dict[str, object]] = []
    if prior is not None:
        smoke = lane_smoke.Smoke(True, "reused")
        stored = prior.get("result_json")
        earlier = cast("Sequence[Mapping[str, object]]", stored.get("checks") or []) if isinstance(stored, Mapping) else []
        result = lane_smoke.result_json(tip, cmds, c.worker, smoke, earlier,
                                        reused_from=str(prior.get("run_id") or ""))
    else:
        if checks:
            lane.log(f"lane: the phase smoke runs at {tip[:12]}: {'; '.join(cmds)}")
            env = checks_env(ctx, {LANE_BASE_VAR: lane.base_sha}, c.phase, SMOKE_SCRATCH)
            try:
                with repo_gate_lock(ctx.repo):
                    results = list(run_checks(lane.phase_worktree, checks, env=env,
                                              log_dir=ctx.runs_dir / ctx.run_id / "checks" / c.phase / SMOKE_SCRATCH,
                                              log_prefix=f"checks/{c.phase}/{SMOKE_SCRATCH}/"))
            finally:
                drop_lane(ctx, c.phase, SMOKE_SCRATCH)
        smoke = lane_smoke.outcome(results)
        result = lane_smoke.result_json(tip, cmds, c.worker, smoke, results)
    row = step_row(run_id=ctx.run_id, step=step_key("phase", "", "smoke", round_), initiative=c.initiative,
                   phase=c.phase, task_id="", kind="smoke", round_=round_, ok=smoke.ok, verdict=smoke.verdict,
                   result=cast(JsonObject, json.loads(json.dumps(result, default=str))), finished_at="",
                   started_at=started, subject_sha=tip, worker=c.worker, claim_epoch=c.epoch)
    written = lane.put([row], {"checks": results})
    lane.log(f"lane: the phase smoke at {tip[:12]} {smoke.verdict}"
             + (f": {' '.join(smoke.reason.split())[:300]}" if smoke.reason else "")
             + ("" if written else "; its row was not written, the claim is gone"))
    return smoke.ok and written


def _schedule(lane: Lane, dag: Mapping[str, Sequence[str]], done: Sequence[str], threads: int) -> dict[str, str]:
    """Edge. Start each task once its needs have rolled up, up to `threads` at once, until none is left to start;
    the tasks that failed, with why. A task blocked behind a failed one is never started."""
    rolled, failed = set(done), dict[str, str]()
    building: dict[Future[tuple[bool, str]], str] = {}
    pool = ThreadPoolExecutor(max_workers=max(threads, 1), thread_name_prefix="lane-task")
    try:
        while True:
            if not lane.stopping():
                for task in lane_plan.next_tasks(dag, rolled, building.values(), threads - len(building), failed):
                    base = lane_rollup.head(lane.phase_worktree)
                    lane.log(f"lane: building {task} on {base[:12]}")
                    building[pool.submit(_task, lane, task, base)] = task
            if not building:
                break
            finished, _ = wait(building, timeout=WATCH_S, return_when=FIRST_COMPLETED)
            for future in finished:
                task = building.pop(future)
                try:
                    ok, detail = future.result()
                except Exception as exc:  # a bug in one task's thread fails that task, never the lane's record
                    ok, detail = False, f"{type(exc).__name__}: {exc}"
                if ok:
                    rolled.add(task)
                else:
                    failed[task] = detail
                    lane.log(f"lane: {task} failed: {' '.join(detail.split())[:300]}")
    except SystemExit:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True, cancel_futures=True)
    for task in lane_plan.blocked(dag, failed):
        failed.setdefault(task, "a task it needs failed")
    return failed


def _base(lane_repo: Path, run_id: str, candidates: Sequence[str]) -> tuple[str, str] | str:
    """Edge. The first candidate base on origin as (branch, sha), or why none could be read."""
    for name in candidates:
        sha, why = lane_rollup.fetch(lane_repo, run_id, name)
        if why:
            return why
        if sha is not None:
            return name, sha
    return f"none of {', '.join(candidates)} is on origin"


def _own_clone(ctx: Ctx, log: Callable[[str], None]) -> Ctx:
    """Edge. The run's context on a clone of its own (`repo_cache.lane_clone_dir`), borrowed from this worker's git
    mirror so it fetches from origin only what the mirror lacks (`worktree.clone_worktree`). The clone's `origin` is
    the shared checkout's, so every fetch and push of the lane goes to origin; the mirror is never a remote, and the
    clone keeps none of its objects borrowed, and it runs the shared checkout's hooks. Only the roll-ups and pushes
    happen in it; the tasks still build in the shared checkout (`Lane.shared`), with its warm pool. The clone's
    context has no pool of its own: pooled worktrees belong to the shared checkout and outlive the run.
    The shared checkout, unchanged, when it names no origin or the clone fails: the lane still runs, slower."""
    ok, said = git_in("-C", str(ctx.repo), "remote", "get-url", "origin")
    if not ok or not said.strip():
        log(f"lane: no origin to clone from, so the lane builds in the shared checkout: {said[:200]}")
        return ctx
    url = repo_cache.absolute_url(said.strip(), ctx.repo)
    dest = repo_cache.lane_clone_dir(ctx.worktree_root, ctx.run_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cloned = clone_worktree(url, dest, cache_root=repo_cache.cache_root(os.environ, os.path.expanduser("~")))
    log(f"lane: {cloned.trace(url)}")
    if not cloned.ok:
        log(f"lane: its own clone failed, so the lane builds in the shared checkout: {cloned.detail[:300]}")
        shutil.rmtree(dest, ignore_errors=True)
        return ctx
    # The shared checkout's hooks still run on the lane's commits and pushes, as they did when the lane built there.
    found, hooks = git_in("-C", str(ctx.repo), "rev-parse", "--path-format=absolute", "--git-path", "hooks")
    if found and hooks.strip():
        git_in("-C", str(dest), "config", "core.hooksPath", hooks.strip())
    return replace(ctx, repo=dest, pool=None)


def _hold_run_lock(ctx: Ctx) -> int:
    """Edge. Take this lane's lock (`repo_cache.lane_lock_file`) and return its descriptor, held until the lane closes
    it: the proof, to any other lane's sweep on this machine, that this run folder is live. The kernel drops it when
    the process dies, however it dies, so no pidfile or clock is consulted."""
    path = repo_cache.lane_lock_file(ctx.worktree_root, ctx.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _try_run_lock(worktree_root: Path, run_id: str) -> int | None:
    """Edge. The run's lane lock taken without waiting: its descriptor, -1 when the run folder has no lock file (a lane
    that died before taking it, or older code), None when a live lane holds it."""
    try:
        fd = os.open(repo_cache.lane_lock_file(worktree_root, run_id), os.O_RDWR)
    except FileNotFoundError:
        return -1
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _sweep_dead_clones(ctx: Ctx) -> list[str]:
    """Edge. Remove the run folder of every dead run that holds a lane clone, whatever its phase: a killed lane's
    clone is a whole checkout, so it does not wait for the next lane of its own phase (`lane_rollup.sweep`). A run is
    dead when its lane lock is free (`_try_run_lock`); each dead one's lock is held while its folder goes."""
    holders = sorted(p.parent.name for p in ctx.worktree_root.glob(f"*/{repo_cache.LANE_CLONE}"))
    taken = {run: _try_run_lock(ctx.worktree_root, run) for run in holders if run != ctx.run_id}
    try:
        dead = repo_cache.dead_clones({run: fd is None for run, fd in taken.items()})
        lane_rollup.sweep_runs(ctx.repo, ctx.worktree_root, dead)
        return dead
    finally:
        for fd in taken.values():
            if fd is not None and fd >= 0:
                os.close(fd)


def run_lane(ctx: Ctx, claim: LaneClaim, conn: Connection, open_conn: Callable[[], Connection],
             items: Sequence[Mapping[str, Any]], base: str, log: Callable[[str], None],
             stop_children: Callable[[], None]) -> int:
    """Edge. Run the claimed phase as the module docstring says; the exit code (`EXIT_*`). On every way out the run's
    worktrees, its own clone (under `<worktree_root>/<run>`, with the private refs it fetched), any private refs in
    the shared checkout, and its progress mark are removed."""
    mark = beat_path(ctx.runs_dir, ctx.run_id)
    fence = Fence(open_conn, claim, log, stop_children, lambda n: write_beat(mark, n))
    phase_worktree = ctx.worktree_root / ctx.run_id / claim.phase / "_phase"
    lane_ctx = ctx
    held = _hold_run_lock(ctx)  # before the clone exists, so no sweep ever sees this run's clone unlocked
    try:
        if not fence.check():
            return EXIT_LOST
        fence.start()
        swept = lane_rollup.sweep(ctx.repo, ctx.worktree_root, ctx.run_id.rpartition("-")[0], ctx.run_id)
        if swept:
            log(f"lane: swept what {', '.join(swept)} left behind")
        dead = _sweep_dead_clones(ctx)
        if dead:
            log(f"lane: removed the clones of dead runs {', '.join(dead)}")
        lane_ctx = _own_clone(ctx, log)
        return _run(lane_ctx, claim, conn, fence, items, base, log, phase_worktree, open_conn,
                    ctx if lane_ctx is not ctx else None)
    finally:
        fence.stop()
        settle_pool(ctx, keep_worktrees=False)
        if phase_worktree.exists():
            remove_worktree(lane_ctx.repo, phase_worktree)
        shutil.rmtree(ctx.worktree_root / ctx.run_id, ignore_errors=True)
        lane_rollup.drop_refs(ctx.repo, ctx.run_id)
        mark.unlink(missing_ok=True)
        os.close(held)  # last: the folder it proved live is gone


def _run(ctx: Ctx, claim: LaneClaim, conn: Connection, fence: Fence, items: Sequence[Mapping[str, Any]], base: str,
         log: Callable[[str], None], phase_worktree: Path, open_conn: Callable[[], Connection],
         shared: Ctx | None = None) -> int:
    tasks = phase_tasks(items, claim.phase)
    row = store_phases.get_row(conn, claim.initiative, claim.phase) or {}
    from harness.landing_store import get_record

    record = get_record(conn, claim.initiative) or {}
    found = _base(ctx.repo, ctx.run_id, base_candidates(str(row.get("base_ref") or ""), str(record.get("branch") or ""),
                                                       claim.initiative, base))
    if isinstance(found, str):
        log(f"lane: no base for {claim.phase}: {found}")
        return EXIT_FAILED
    base_branch, base_sha = found
    branch = str(row.get("branch") or "") or lane_rollup.branch_name(claim.initiative, claim.phase)
    tip, why = lane_rollup.fetch(ctx.repo, ctx.run_id, branch)
    if why:
        log(f"lane: {why}")
        return EXIT_FAILED
    tip = tip or base_sha
    ok, text = lane_rollup.log_since(ctx.repo, base_sha, tip)
    if not ok:
        log(f"lane: the phase branch's log could not be read: {text[:300]}")
        return EXIT_FAILED
    progress = lane_progress.read(text, claim.initiative, claim.phase)
    for sha in progress.ignored:
        log(f"lane: {sha[:12]} on {branch} is not a task commit of {claim.initiative}/{claim.phase}; ignored")
    lane = Lane(ctx, claim, conn, fence, phase_worktree, branch, base_sha, {str(t["id"]): t for t in tasks}, log,
                open_conn, shared=shared)
    try:
        _origin_progress(lane, tip)
        order = [str(t["id"]) for t in tasks]
        nxt = lane_progress.resume_plan(order, list(progress.done),
                                        store_phases.last_round(conn, claim.initiative, claim.phase), progress.revisions)
        log(f"lane: {claim.initiative}/{claim.phase} on {branch} from {base_branch}: "
            f"{len(progress.done)} of {len(order)} task(s) on the branch; next: {nxt.action}"
            + (f" {', '.join(nxt.tasks)}" if nxt.tasks else ""))
        if nxt.action not in ("build", "review"):
            return EXIT_BUILT
        ok, detail = lane_rollup.open_phase_worktree(ctx.repo, phase_worktree, tip)
        if not ok:
            log(f"lane: the phase worktree could not be opened at {tip[:12]}: {detail[:300]}")
            return EXIT_FAILED
        failed = _schedule(lane, task_dag(tasks), list(progress.done), ctx.max_parallel) if nxt.action == "build" else {}
        smoked = None
        if smoke_due(failed, lane.stopping()):
            smoked = _smoke(lane, lane_rollup.head(phase_worktree), smoke_round(nxt))
    finally:
        lane.close()
    for task, why in sorted(failed.items()):
        log(f"lane: {task} not rolled up: {' '.join(why.split())[:300]}")
    if fence.lost.is_set():
        log(f"lane: the claim was lost ({fence.why}); nothing more was written or pushed")
    return exit_code(fence.lost.is_set(), sorted(failed), smoked is not False)


def beat_path(runs_dir: Path, run_id: str) -> Path:
    """Pure. The lane's progress mark, beside its pidfile: the worker reads it (`worker.lane_stalled`)."""
    return runs_dir / f"{run_id}.lane-beat"


def write_beat(path: Path, count: int) -> None:
    """Edge. Rewrite the progress mark with `count`, whole or not at all."""
    part = path.with_name(path.name + ".part")
    part.write_text(f"{count}\n", encoding="utf-8")
    os.replace(part, path)


def terminate_groups(pids: Sequence[int]) -> None:
    """Edge. TERM each of the lane's children and, for one that leads a process group of its own (a check runs in a
    session of its own), its whole group, so a shell's grandchildren stop too and nothing holds a pipe open. A child
    in the lane's own group is signalled alone: the lane's group is never signalled."""
    for pid in pids:
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)


def lanes_of(environ: Mapping[str, str]) -> int:
    """Pure. The lanes this machine runs, as a local launch states them (`COX_LANE_CAPACITY`); 1 when it states none."""
    text = environ.get(LANE_CAPACITY_VAR, "").strip()
    return int(text) if text.isdigit() and int(text) > 0 else 1


def threads_for(conn: Connection, worker: str, environ: Mapping[str, str]) -> int:
    """Edge. This lane's task threads: the worker's hosts row's `threads` (0 or none: the cores), split among the
    lanes the machine runs (`lanes_of`)."""
    return lane_plan.threads(store_phases.host_threads(conn, worker), os.cpu_count() or 1, lanes_of(environ))
