"""The phase lane: one claimed phase, its tasks built in parallel, each rolled up onto the phase branch as it passes.

A worker claims a phase (`store_phases.claim_next`) and starts this lane under the claim's epoch (`shell.py phase-lane
--lane-worker W --claim-epoch E`, through `cox route launch epic --lane-worker`). The lane:

1. checks it still holds the claim, and keeps checking on a thread of its own (`Fence`) on a connection of its own;
2. reads where the phase branch stands on origin (`lane_rollup.fetch`) and what it already holds (`lane_progress.read`
   of the `Cox-*` trailers): git is the authority for what is built, so a lane that claims after another finds the
   tasks already rolled up and builds only the rest; a roll-up whose step row was lost is written again;
3. joins that with the phase's last review round (`lane_progress.resume_plan`): with tasks missing it builds them; a
   phase with every task rolled up waits for its review, which is L6's, so the lane ends there;
4. starts each task once everything it needs inside the phase has rolled up (`lane_plan.next_tasks`), up to its share
   of the machine's threads (`lane_plan.threads`), each in a worktree of its own cut from the phase tip at that moment;
5. per task: the lifecycle graph in build-only mode (plan, build, handoff; never a review, which is the phase's), then
   the cheap checks in the task's worktree (the cartridge's and `.agent-checks`' task checks, narrowed to what the
   change touches: unit tests, lint, the privacy check), with the task's own verification commands;
6. the roll-up, in order: the checks passed, their artifacts uploaded, the `checks` step row written; the phase's local
   lock taken; the change applied cleanly onto the phase tip (a conflict fails the task: resolving one is L5's) and
   committed with the trailers; the claim checked again; pushed; the `rollup` step row written with the commit.

Every step row is written only while the claim is held (`store_phases.put_steps_fenced`, one transaction under a share
lock on the claim row), and every push only after the claim was checked and as a fast-forward, so a lane that lost its
claim (a stop-then-continue, a worker gone quiet) writes nothing and pushes nothing. The lane stops itself, its agents
and checks with it, once its claim is lost or its lease is within `phase_pick.LANE_MARGIN_S` of lapsing by the store's
clock: by then its worker has gone the 45 s it may go without a heartbeat, so the lane is gone before the lease lapses
and another lane can be started. The lane takes no `leases` row (the claim is its lease), opens the store through
`store_scoped.open_scoped` as the worker's scoped role, and never writes `work_items`, the initiative branch or the
landing record; the chair turns the roll-ups into approved tasks when the phase is approved.

The rules are pure (`lane_plan`, `lane_progress`, `lane_rollup.message`, and the helpers here marked pure); the rest is
the edge.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, cast

from graphs._contract import ContractViolation
from harness import lane_plan, lane_progress, lane_rollup, phase_pick, store_phases
from harness.artifacts import step_artifacts
from harness.epic_branches import LIFECYCLE
from harness.epic_build import build_task, drop_lane, lifecycle_invocation, settle_pool, verify_of
from harness.epic_context import Ctx, git_in
from harness.handoff import plan_context
from harness.lane_workers import LANE_CAPACITY_VAR
from harness.store_dialect import Connection, JsonObject
from harness.store_steps import step_key, step_row, steps_of_result, with_checks
from harness.store_types import StepRow
from harness.worktree import remove_worktree
from runner.protocol import LimitStop, RunnerError

__all__ = [
    "EXIT_BUILT",
    "EXIT_FAILED",
    "EXIT_LOST",
    "Fence",
    "LaneClaim",
    "TaskCtx",
    "base_candidates",
    "exit_code",
    "lane_url",
    "lanes_of",
    "missing_rollups",
    "phase_tasks",
    "run_lane",
    "task_dag",
    "terminate_groups",
    "threads_for",
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


def exit_code(lost: bool, failed: Sequence[str]) -> int:
    """Pure. The lane's exit: lost beats failed beats built."""
    return EXIT_LOST if lost else EXIT_FAILED if failed else EXIT_BUILT


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
                 on_lost: Callable[[], None]) -> None:
        self._open, self._claim, self._log, self._on_lost = open_conn, claim, log, on_lost
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
        def loop() -> None:
            while not self._halt.wait(WATCH_S) and self.check():
                pass

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
    items: Mapping[str, Mapping[str, Any]]
    log: Callable[[str], None]
    store_lock: threading.Lock = field(default_factory=threading.Lock)
    handoffs: dict[str, Mapping[str, Any]] = field(default_factory=dict[str, Mapping[str, Any]])
    halted: threading.Event = field(default_factory=threading.Event)

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
        with self.store_lock:
            listed = (
                step_artifacts(self.ctx.artifacts, self.conn, rows, result, runs_dir=self.ctx.runs_dir,
                               run_id=self.ctx.run_id, phase=c.phase, task=rows[0]["task_id"])
                if result is not None and rows else tuple(rows)
            )
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


def _rows_of(lane: Lane, result: Mapping[str, object], task: str, started: str) -> tuple[StepRow, ...]:
    c = lane.claim
    rows = steps_of_result(result, run_id=lane.ctx.run_id, initiative=c.initiative, phase=c.phase, task=task, at="",
                           claim_epoch=c.epoch, worker=c.worker)
    return tuple(cast(StepRow, {**r, "started_at": started}) for r in rows)


def _build(lane: Lane, task: str, base: str) -> tuple[Mapping[str, object] | None, str, str, TaskCtx]:
    """Edge. The task's build-only lifecycle run, with its plan and build rows written: (result, why not, start)."""
    started = lane.now()
    tctx = TaskCtx.of(lane.ctx, base)
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


def _cleanup(lane: Lane, tctx: TaskCtx, task: str) -> None:
    """Edge. The task's database and scratch, then its worktree (a pooled one goes back with the pool) and branch."""
    phase = lane.claim.phase
    drop_lane(tctx, phase, task)
    if tctx.pool is None:
        remove_worktree(tctx.repo, tctx.task_worktree(phase, task))
        git_in("-C", str(tctx.repo), "branch", "-D", tctx.scratch_branch(task))


def _roll_up(lane: Lane, task: str, base: str, tctx: TaskCtx, started: str) -> tuple[bool, str]:
    """Edge. The roll-up under the phase's local lock: apply, commit, check the claim, push, then the `rollup` row."""
    c = lane.claim
    worktree = tctx.task_worktree(c.phase, task)
    with lane_rollup.phase_lock(lane.ctx.repo, c.initiative, c.phase):
        if lane.stopping():
            return False, "the lane is stopping"
        ok, diff = lane_rollup.change(worktree, base)
        if not ok:
            return False, f"the task's change could not be read: {diff[:300]}"
        tip = lane_rollup.head(lane.phase_worktree)
        title = str(lane.items[task].get("title") or task)
        sha, why = lane_rollup.apply_and_commit(
            lane.phase_worktree, diff, lane_rollup.message(c.initiative, c.phase, task, lane.ctx.run_id, title),
        )
        if sha is None:
            lane.put([lane.row(task, "rollup", False, "conflict", {"reason": why[:500], "tip": tip}, started)])
            return False, why
        if not lane.fence.check():
            lane_rollup.reset_to(lane.phase_worktree, tip)
            return False, "the claim was lost before the push"
        pushed, detail = lane_rollup.push(lane.phase_worktree, lane.branch)
        if not pushed:
            lane_rollup.reset_to(lane.phase_worktree, tip)
            lane.fence.lose(f"origin refused the push of {lane.branch}: {detail[:300]}")
            return False, f"the push was refused: {detail[:300]}"
        lane.log(f"lane: {task} rolled up as {sha[:12]} on {lane.branch}")
        lane.put([lane.row(task, "rollup", True, "pushed", {"base": base, "parent": tip}, started, sha)])
    return True, sha


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


def run_lane(ctx: Ctx, claim: LaneClaim, conn: Connection, open_conn: Callable[[], Connection],
             items: Sequence[Mapping[str, Any]], base: str, log: Callable[[str], None],
             stop_children: Callable[[], None]) -> int:
    """Edge. Run the claimed phase as the module docstring says; the exit code (`EXIT_*`)."""
    fence = Fence(open_conn, claim, log, stop_children)
    if not fence.check():
        return EXIT_LOST
    fence.start()
    phase_worktree = ctx.worktree_root / ctx.run_id / claim.phase / "_phase"
    try:
        return _run(ctx, claim, conn, fence, items, base, log, phase_worktree)
    finally:
        fence.stop()
        settle_pool(ctx, keep_worktrees=False)
        if phase_worktree.exists():
            remove_worktree(ctx.repo, phase_worktree)


def _run(ctx: Ctx, claim: LaneClaim, conn: Connection, fence: Fence, items: Sequence[Mapping[str, Any]], base: str,
         log: Callable[[str], None], phase_worktree: Path) -> int:
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
    lane = Lane(ctx, claim, conn, fence, phase_worktree, branch, {str(t["id"]): t for t in tasks}, log)
    recovered = missing_rollups(progress.done, store_phases.rollup_rows(conn, [claim.initiative]), claim.phase)
    if recovered:
        start = lane.now()
        lane.put([lane.row(t, "rollup", True, "recovered", {"recovered": True}, start, sha) for t, sha in recovered.items()])
    order = [str(t["id"]) for t in tasks]
    nxt = lane_progress.resume_plan(order, list(progress.done), store_phases.last_round(conn, claim.initiative, claim.phase),
                                    progress.revisions)
    log(f"lane: {claim.initiative}/{claim.phase} on {branch} from {base_branch}: "
        f"{len(progress.done)} of {len(order)} task(s) on the branch; next: {nxt.action}"
        + (f" {', '.join(nxt.tasks)}" if nxt.tasks else ""))
    if nxt.action != "build":
        return EXIT_BUILT
    ok, detail = lane_rollup.open_phase_worktree(ctx.repo, phase_worktree, tip)
    if not ok:
        log(f"lane: the phase worktree could not be opened at {tip[:12]}: {detail[:300]}")
        return EXIT_FAILED
    dag = task_dag(tasks)
    failed = _schedule(lane, dag, list(progress.done), ctx.max_parallel)
    for task, why in sorted(failed.items()):
        log(f"lane: {task} not rolled up: {' '.join(why.split())[:300]}")
    if fence.lost.is_set():
        log(f"lane: the claim was lost ({fence.why}); nothing more was written or pushed")
    return exit_code(fence.lost.is_set(), sorted(failed))


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
