"""The epic driver's entry point: `run_epic`, the lanes it runs, and quarantining a task."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

from core import workstore
from core.cartridge import test_database_setting
from core.landing import is_initiative, resolve_land_at
from harness.checks import repo_checks
from harness.courier_adapter import send as send_to_courier
from harness.epic_batch import audit_approved_kept
from harness.epic_branches import (
    bring_forward,
    merge_into_initiative,
    parent_gap,
    resolve_default_ref,
    run_scratch,
    start_initiative,
)
from harness.epic_build import checked_work_state, drop_lane, settle_pool
from harness.epic_context import Ctx, PoolSetting, StoreRetry, declared_phases, unreached_warning
from harness.epic_phase import run_phase_tasks
from harness.epic_phase_branch import awaiting_land_lines, blocked_line, outside_parents_complete
from harness.epic_records import (
    LEASE_NAME,
    build_result,
    clear_pause_if_held,
    epic_log,
    mirror_read,
    publish_run_exit,
    read_store_rows,
    record_phase,
    record_run_exit,
    store_fresh,
    unlanded_tasks,
)
from harness.epic_triage import close_runner
from harness.epic_types import PhaseRecord, Quarantine, TaskEntry
from harness.lane_workers import lane_workers_from
from harness.lanes import lane_bound
from harness.scheduler import plan_launches
from harness.store_write import Store
from harness.waves import compute_waves, parent_lines, phase_parents, unreached_phases, widest_wave
from harness.worktree import keep_worktree, remove_worktree
from runner.protocol import LimitStop


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
    retry: StoreRetry | None = None,
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
    retry = retry or StoreRetry.for_store(store)
    repo = Path(repo).expanduser()
    ctx: Ctx | None = None
    result: dict[str, Any] | None = None
    # Bound before the `try` so the `finally` can build a partial result from whatever phases finished.
    phases: list[PhaseRecord] = []
    tasks: list[TaskEntry] = []
    quarantined: list[Quarantine] = []
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
        ctx = Ctx(
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
            test_database=cast(Mapping[str, str] | None, test_database_setting(cartridge)),
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
        mirror_read(ctx, items)
        if is_initiative(ctx.land_at):
            # Before any phase: the initiative branch is cut from its parent and recorded, so every phase forks from it.
            ctx = replace(ctx, initiative_branch=start_initiative(ctx, initiative))
            # Then the branch takes its parent forward, so no phase builds on an old base (H4.2).
            ctx = replace(ctx, parent_behind=bring_forward(ctx, items))

        parents = phase_parents(items)
        named = None if only_phases is None else frozenset(only_phases)
        seed: frozenset[str] = frozenset()
        unmerged: dict[str, tuple[str, str]] = {}
        if named is not None:
            seed = frozenset(outside_parents_complete(items, read_store_rows(ctx), parents, named, ctx.land_at))
            if ctx.initiative_branch is not None:
                # With an initiative branch a parent outside the run counts only once it is merged into that branch.
                unmerged = {p: gap for p in sorted(seed) if (gap := parent_gap(ctx, p, items)) is not None}
                seed = seed - set(unmerged)
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

        def add_phase(record: PhaseRecord) -> None:
            phases.append(record)
            record_phase(ctx, record)
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
                    gap, branch = unmerged.get(missing[0], ("", ""))
                    missing_branch: PhaseRecord = {"missing_branch": branch} if branch else {}
                    add_phase(
                        {
                            "phase": phase,
                            "status": "blocked",
                            "parents": sorted(parents[phase]),
                            "reason": gap or (
                                f"parent phase '{missing[0]}' is outside this run and its tasks are not all met "
                                f"under land_at {ctx.land_at}; dependents do not run"
                            ),
                            **missing_branch,
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
        audit_approved_kept(ctx, {str(i["id"]): i for i in items}, tasks, quarantined)
        result = build_result(
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
            clear_pause_if_held(store, run_id, lease_name or LEASE_NAME, epoch, retry)
        finally:
            # Once, after the phase loop has drained: a phase's threads stay open while any other phase can still use them.
            close_error = close_runner(ctx.runner) if ctx is not None else None
            # A raise leaves no result, so the outcome is built from the phases that finished. A failure
            # building it is logged and must not replace the exception already in flight.
            if result is None and ctx is not None:
                try:
                    result = build_result(
                        run_id=run_id, date=date, initiative_id=ctx.initiative_id, phases=phases, tasks=tasks,
                        quarantined=quarantined, proposals=proposals, stacks_rebased=stacks_rebased,
                        paused_until=paused_until, repo=repo,
                        land_at=ctx.land_at, lane_databases=ctx.lane_drops if ctx.test_database else None,
                    )
                except Exception as exc:
                    epic_log.warning("partial result for %s not built: %s: %s", run_id, type(exc).__name__, exc)
            # A return leaves no exception in flight here; a raise, a signal or an exit leaves one.
            exit_status = "ok" if sys.exc_info()[0] is None and close_error is None else "error"
            record_run_exit(store, run_id, exit_status, retry, result)
            publish_run_exit(run_id, ctx.initiative_id if ctx is not None else "", exit_status)
        # Nothing before `ctx` exists can have made a worktree, so there is nothing to do yet.
        if ctx is not None:
            run_dir = ctx.worktree_root / ctx.run_id
            settle_pool(ctx, keep_worktrees=keep_worktrees)
            # The phase-level and end-of-run scratch directories, outside the run directory, so a kept run never carries them.
            shutil.rmtree(run_scratch(ctx), ignore_errors=True)
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
                    drop_lane(ctx, phase, task)
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
    whole_parents: bool = False,
) -> list[dict[str, Any]]:
    """`items` as the planner sees them: a ready task is held while its phase has an incomplete parent or it already launched.

    A task whose needs are all merged is not held for an incomplete parent, unless a parent has finished without
    meeting its goal (`failed`): its dependents do not run. With `whole_parents` (an initiative branch) no task
    starts early: a phase is cut from the initiative branch, which holds a parent only once the whole parent merged.
    """
    closed = {p for p, ps in parents.items() if not ps <= complete}
    shut = {p for p, ps in parents.items() if ps & set(failed)}
    by_id = {str(i["id"]): i for i in items}

    def state(item: Mapping[str, Any]) -> Any:
        if item.get("state") != "ready":
            return item.get("state")
        if str(item["id"]) in launched:
            return "in_progress"
        return "blocked" if str(item.get("phase")) in shut or (str(item.get("phase")) in closed and (whole_parents or not _needs_merged(item, by_id))) else "ready"

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
    ctx: Ctx,
    *,
    initiative: Mapping[str, Any],
    items: list[dict[str, Any]],
    parents: Mapping[str, set[str]],
    waves: Sequence[Sequence[str]],
    add_phase: Callable[[PhaseRecord], None],
    phases: list[PhaseRecord],
    tasks: list[TaskEntry],
    quarantined: list[Quarantine],
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
    running: dict[Future[PhaseRecord], tuple[str, frozenset[str], int]] = {}
    lane_of: dict[str, int] = {}
    acc: dict[str, PhaseRecord] = {}
    launched: set[str] = set()
    complete: set[str] = set(seed_complete)
    recorded = {str(r["phase"]) for r in phases}
    serialised: dict[str, str] = {}
    why_stopped: list[str] = []

    def settle(phase: str, record: PhaseRecord) -> None:
        record = {**record, "wave": wave_of.get(phase), "lane": lane_of.get(phase)}
        if record["status"] == "awaiting_land" and adopt_done(phase):
            # The store keeps the first row of a phase, so a phase done elsewhere is recorded complete the first time.
            # Only an awaiting_land record is rewritten: a partial one carries a verdict this check must not overrule.
            record = PhaseRecord(**record)
            record.pop("awaiting_tasks", None)
            record.update(status="complete", reason="every task in the phase is done in the store")
        if phase in serialised:
            record["serialised"] = serialised[phase]
        if record["status"] in ("complete", "awaiting_land") and is_initiative(ctx.land_at):
            # A built phase merges into the initiative branch here, locally: that is "phase complete", and only then
            # may the phases that need it start. Its tasks stay approved; they are done when the initiative lands.
            merge = merge_into_initiative(ctx, phase, items)
            if merge is not None:
                record["initiative_merge"] = merge
                if merge["status"] == "merged":
                    record = PhaseRecord(**record)
                    record.pop("awaiting_tasks", None)
                    if record["status"] == "awaiting_land":
                        record.update(status="complete", reason=f"merged into {merge['branch']}")
                elif merge["status"] == "not_built":
                    record.update(status="partial", reason=f"phase {phase} is not merged into {merge['branch']}: {merge['detail']}")
                else:
                    what = merge.get("detail") or merge["status"]
                    record.update(status="awaiting_land", reason=f"phase {phase} is built; its merge into {merge['branch']} did not go through: {what}")
        add_phase(record)
        recorded.add(phase)
        if record["status"] == "complete":
            complete.add(phase)

    def adopt_done(parent: str) -> bool:
        """True after adopting the rows of `parent`, when every one of its tasks is done in the store."""
        rows = read_store_rows(ctx)
        with ctx.items_lock:
            mine = [i for i in items if i.get("phase") == parent]
            fresh = store_fresh(mine, rows)
            if not fresh or any(i["state"] not in ("done", "dropped") for i in fresh):
                return False
            for item, new in zip(mine, fresh, strict=True):
                item.update({k: new[k] for k in ("state", "merged", "merged_commit")})
        return True

    def parent_block(phase: str) -> PhaseRecord | None:
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
                f"parent phase '{bad}' is built but not merged into {parent_record['initiative_merge'].get('branch')}: "
                f"{parent_record['initiative_merge'].get('detail') or parent_record['initiative_merge'].get('status')}"
                if parent_record.get("status") == "awaiting_land" and parent_record.get("initiative_merge")
                else f"parent phase '{bad}' is not merged into {parent_record['initiative_merge'].get('branch')}: "
                f"{parent_record['initiative_merge'].get('detail')}; dependents do not run"
                if parent_record.get("initiative_merge")
                else f"parent phase '{bad}' is awaiting the land of {waiting} approved task(s)"
                if parent_record.get("status") == "awaiting_land"
                else f"parent phase '{bad}' did not meet its goal; dependents do not run"
            ),
        }

    def fold(fut: Future[PhaseRecord]) -> None:
        phase, _ids, _lane = running.pop(fut)
        try:
            record = fut.result()
        except LimitStop as exc:
            tally.paused_until = exc.detail.split("resets", 1)[-1].strip()
            why_stopped.append(f"account session limit, resets {tally.paused_until}")
            return
        phase_tasks = record.pop("task_records")
        tasks.extend(phase_tasks)
        for t, command in unlanded_tasks(phase_tasks, ctx.run_id, ctx.repo):
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
                run_phase_tasks, ctx, initiative=initiative, phase=phase,
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
                    view = _open_view(
                        items, parents, complete, launched, {p for p in recorded if p not in complete},
                        whole_parents=ctx.initiative_branch is not None,
                    )
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
