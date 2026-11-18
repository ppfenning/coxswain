"""`cox runs land` and `land-batch`: the walk over a run's tasks, the leases that guard it, and the initiative's target branch."""

from __future__ import annotations

import argparse
import contextlib
import datetime
import importlib
import importlib.metadata
import importlib.util
import json
import os
import socket
import sys
import threading
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path

import yaml

from agent_tools.cli_common import _profile_path, _read_text_or_none
from agent_tools.cli_lake import _lake_provider, _lake_real_run
from agent_tools.cli_land_facts import (
    _backfill_phase_task_files,
    _carried_files,
    _carried_walk,
    _initiative_land_facts,
    _initiative_of_phase,
    _initiative_title,
    _land_branch_counts,
    _land_branches,
    _land_enrich,
    _land_leftover,
    _land_load,
    _land_parent_refused,
    _land_phase_done,
    _land_phase_record,
    _land_rebuild,
    _land_record,
    _land_recreate,
    _land_resume,
    _last_phase_with_manifest,
    _merged_line,
    _merged_pr_ref,
    _parent_land_refusal,
    _parent_phase,
    _phase_items,
    _phase_merged_pr,
    _phase_needing_land,
    _phase_pr_facts,
    _phase_task_facts,
    _phase_task_items,
    _task_already_on_main,
    _ticket_items,
    _ticket_present,
    _with_check_cap,
)
from agent_tools.cli_land_steps import (
    _GENERATE_FAILED,
    _close_review_prs,
    _execute_land_step,
    _git_out,
    _land_item_facts,
    _land_reference,
    _land_worktree,
    _landed_pr,
    _landing_ref,
    _mark_done_facts,
    _mark_remaining_done,
    _merge_updating,
    _open_pr_ref,
    _pr_number,
    _remove_land_worktree,
    _repo_is_dirty,
    _step_initiative,
    _sync_default_branch,
    _taken_run_names,
)
from agent_tools.cli_lazy import (
    base_branch,
    bounded_run,
    chair,
    chair_facts,
    forge,
    forge_github,
    install_root,
    jsonl_log,
    lake_config,
    lake_lease,
    land,
    land_batch_effects,
    land_batch_group,
    land_batch_run,
    land_batch_settings,
    land_checkout_guard,
    land_lease,
    land_limit,
    land_merge_lock,
    land_repo_lease,
    queue_export,
    remote_lane,
    repo_clone,
    review_pr,
    route,
    route_edit,
    run_store,
    store_cli,
    store_url,
    work_state,
)
from agent_tools.cli_route_sync import _initiative_land_at, _local_max_in_flight, _resolved_tracker, _team_cartridge
from agent_tools.cli_runs import _leader_heartbeat_minutes, _resolved_run_id
from agent_tools.cli_runs_fetch import (
    _UNLABELED,
    _holder_label,
    _initiative_of,
    _land_refetch_phase_branch,
    _runs_dir_for_land,
)
from agent_tools.cli_stats import _resolve_profile_or_refuse
from agent_tools.cli_usage import _resolved_gate_level


def _runs_review(a: argparse.Namespace) -> int:
    profile, rc = _resolve_profile_or_refuse(a)
    if rc is not None:
        return rc
    runs_dir = Path(profile["workspace_dir"]).expanduser() / "runs"
    run_id = route.next_run_id(_taken_run_names(runs_dir), "review")
    return review_pr.run_review(a.pr, profile, run_id, lambda argv: bounded_run.completed(argv, capture_output=True, text=True))


_LAND_LEASE_TTL_SECONDS = 3600
_GUARDED_STEPS = ("merge", "mark_done", "pr_branch_cut", "merge_phase", "pr_update", "pr_ready", "mark_merged", "phase_done")
_PR_REPORT_STEPS = ("pr_create", "pr_update", "pr_ready")  # a failure after the push is reported; mark_merged still runs
_AFTER_PR_FAILURE = (*_PR_REPORT_STEPS, "mark_merged", "phase_done")


def _land_approval_stop(mode: str, store_state: str | None, file_state: str | None) -> str | None:
    """Pure. None when the state the approved check reads is `approved`, else the reason to stop, naming that state."""
    state = land.approved_state(mode, store_state, file_state)
    if state == "approved":
        return None
    if state == "done":
        return "land: task is already done; another machine landed it, so nothing is merged"
    return f"land: task state is {state!r}, not approved; nothing is merged"


def _land_store_stop(runs_dir: Path, record: dict, item_path: str | None) -> str | None:
    """Edge. The store's state and the item's, through the approved check; prints which of the two decided.

    `task_state_of` reads a missing row and a failed read alike as None, so the file line names both causes."""
    if item_path is None:
        return "land: refusing, the record names no initiative, so work_state store has no task to check or lease"
    initiative, task = record["initiative"], record["task"]
    store_state = run_store.task_state_of(runs_dir, initiative, task)
    text = _read_text_or_none(Path(item_path))
    file_state = route.parse_frontmatter(text)[0].get("state") if text else None
    source = "store" if store_state is not None else f"file (the store returned no row for {initiative}/{task}: none recorded, or the read failed)"
    print(f"land: state from {source}", file=sys.stderr)
    return _land_approval_stop("store", store_state, file_state)


def _task_lease_stop(task: str, kind: str, result: store_cli.LeaseResult) -> str | None:
    """None when the `land:<task>` lease renewed, else the reason to stop before `kind`."""
    if isinstance(result, store_cli.LeaseGranted):
        return None
    lost = f"held by {result.holder} at epoch {result.epoch}" if isinstance(result, store_cli.LeaseRefused) else (
        result.detail if isinstance(result, store_cli.LeaseError) else "the store is not available")
    return f"land: refusing {kind}, the land lease for {task} could not be renewed: {lost}"


# The ttl `_land` passes to land_repo_lease.acquire. A renew restarts it, but only when one runs: `_LeaseGuard.renewing`
# renews every third of it for as long as the land runs, because a step such as wait_checks blocks for longer than it.
_REPO_LEASE_TTL_SECONDS = 1200
_LAND_LOCK_POLL_SECONDS = 5  # `runs land --wait-lock` retries the repo lease this often
# The merge lock is held for one state read and one merge, not the waits between them, so its ttl is short and a land
# that cannot take it within the timeout stops for the chair to retry.
_MERGE_LOCK_TTL_SECONDS = 120
_MERGE_LOCK_TIMEOUT_SECONDS = 60


def _merge_lock_factory(runs_dir: Path, repo: str, holder: str, limit: int, leased: bool) -> Callable[[], contextlib.AbstractContextManager] | None:
    """The merge lock for `repo`, or None. With a limit of 1 the repo lease already admits one land, so no lock is taken;
    without a repo lease there is no store to take it in."""
    if limit <= 1 or not leased:
        return None
    return lambda: land_merge_lock.hold(runs_dir, repo, holder, _MERGE_LOCK_TTL_SECONDS, _MERGE_LOCK_TIMEOUT_SECONDS)


def _lease_standing(result: store_cli.LeaseResult) -> str:
    """Pure. `kept` on a grant, `refused` on any refusal, else `unrenewable`.

    A refused renew does not say whether the lease lapsed or another holder took it. The store answers both with no
    holder and no epoch: `harness.store_api.lease_renew_on` answers a refusal `LeaseRefused(None, None)`,
    the `held by None at epoch None` of the 2026-10-30 land that waited on CI.
    Only an acquire names a holder, and it refuses while another live holder has the lease. So a refused renew is settled
    by acquiring: a grant means the lease had lapsed with no other holder, a refusal names who holds it."""
    if isinstance(result, store_cli.LeaseGranted):
        return "kept"
    if isinstance(result, store_cli.LeaseRefused):
        return "refused"
    return "unrenewable"


def _renew_loop(beat: Callable[[], None], interval: float, stop: threading.Event) -> None:
    """Calls `beat` every `interval` seconds until `stop` is set. A beat that raises is dropped: the guard's own renew before the next step reports it."""
    while not stop.wait(interval):
        with contextlib.suppress(Exception):
            beat()


def _repo_lease_stop(repo: str, kind: str, result: store_cli.LeaseResult, renewed_at: float, now: float, ttl: int) -> str | None:
    """None when the repo lease renewed, else the reason to stop before `kind`. Clock values are seconds on one clock."""
    if isinstance(result, store_cli.LeaseGranted):
        return None
    if isinstance(result, store_cli.LeaseRefused):
        late = f", {int(now - renewed_at)}s since the last renewal against a {ttl}s ttl" if now - renewed_at > ttl else ""
        return f"land: refusing {kind}, the repo lease for {repo} was lost: held by {result.holder} at epoch {result.epoch}{late}"
    if isinstance(result, store_cli.LeaseError):
        why = result.detail
    elif isinstance(result, store_cli.NotAvailable):
        why = result.reason or "the store is not available"
    else:
        why = "it was released"
    return f"land: refusing {kind}, the repo lease for {repo} could not be renewed: {why}"


class _LeaseGuard:
    """Edge. Keeps one lease renewed: before each step it guards, and every third of `ttl` while `renewing` is entered.

    A refused renew is settled by `reacquire`, which only succeeds when no other live holder has the lease: a grant means it
    lapsed untaken, and `say` notes it. A refusal from the acquire names the holder; with no `reacquire` the renew's own refusal stands.
    Either refusal is final: it is remembered, appended to `lost` once, and every later call returns the stop.
    A transient failure on the timer is not remembered; the next guarded step renews again and reports it."""

    def __init__(self, label: str, renew: Callable[[], store_cli.LeaseResult], reacquire: Callable[[], store_cli.LeaseResult] | None,
                 clock: Callable[[], float], ttl: int, stop_for: Callable[[str, store_cli.LeaseResult, float, float], str | None],
                 lost: list[str] | None = None, only: Collection[str] | None = None,
                 say: Callable[[str], None] = print, new_stop: Callable[[], threading.Event] = threading.Event) -> None:
        self.interval = max(1, ttl // 3)
        self._label, self._renew, self._reacquire, self._clock = label, renew, reacquire, clock
        self._stop_for, self._lost, self._only, self._say, self._new_stop = stop_for, lost, only, say, new_stop
        self._renewed = clock()
        self._failed: tuple[store_cli.LeaseResult, float, float] | None = None  # the result, the last renewal, the time of failure
        self._lock = threading.Lock()

    def _attempt(self) -> store_cli.LeaseResult | None:
        result = self._renew()
        if _lease_standing(result) == "refused" and self._reacquire is not None:
            result = self._reacquire()
            if isinstance(result, store_cli.LeaseGranted):
                self._say(f"land: the {self._label} expired with no holder; re-acquired it at epoch {result.epoch}, continuing")
        return None if isinstance(result, store_cli.LeaseGranted) else result

    def _check(self, kind: str, background: bool) -> str | None:
        with self._lock:
            if self._failed is None:
                now = self._clock()
                result = self._attempt()
                if result is None:
                    self._renewed = now
                elif not background or isinstance(result, store_cli.LeaseRefused):
                    self._failed = (result, self._renewed, now)
                    if isinstance(result, store_cli.LeaseRefused) and self._lost is not None:
                        self._lost.append(self._stop_for(kind, result, self._renewed, now) or "")
            if self._failed is None:
                return None
            result, renewed, now = self._failed
        return self._stop_for(kind, result, renewed, now)

    def __call__(self, kind: str) -> str | None:
        return None if self._only is not None and kind not in self._only else self._check(kind, False)

    def beat(self) -> str | None:
        """One timer renewal. Only a refusal is remembered; a store error is left for the next guarded step to renew again."""
        return self._check("the wait", True)

    @contextlib.contextmanager
    def renewing(self):
        """Renews on a background thread for the length of the `with`, so a step that blocks past the ttl still keeps the lease."""
        stop = self._new_stop()
        thread = threading.Thread(target=_renew_loop, args=(self.beat, self.interval, stop), daemon=True)
        thread.start()
        try:
            yield self
        finally:
            stop.set()
            thread.join()


def _repo_lease_guard(repo: str, renew: Callable[[], store_cli.LeaseResult], clock: Callable[[], float], ttl: int,
                      lost: list[str], reacquire: Callable[[], store_cli.LeaseResult] | None = None) -> _LeaseGuard:
    """Edge. A walk guard that renews the repo lease before every step, not only the guarded ones.

    A refusal means another holder owns the lease: it is appended to `lost` so the caller does not release it."""
    return _LeaseGuard(f"repo lease for {repo}", renew, reacquire, clock, ttl,
                       lambda kind, result, renewed, now: _repo_lease_stop(repo, kind, result, renewed, now, ttl), lost)


def _first_stop(first: Callable[[str], str | None], then: Callable[[str], str | None] | None) -> Callable[[str], str | None]:
    """A guard that asks `first`, and asks `then` only when `first` lets the step run."""
    return lambda kind: first(kind) or (then(kind) if then is not None else None)


_LandWalk = Callable[[Callable[[str], str | None] | None], tuple[int, list[str], str]]


def _land_walked(a: argparse.Namespace, runs_dir: Path, mode: str, task: str | None, recheck: Callable[[], str | None],
                 walk: _LandWalk) -> tuple[int, list[str], str] | None:
    """`walk(None)` under `files`. Under `store` `recheck` runs again inside the `land:<task>` lease, and
    the walk renews the lease before `merge` and `mark_done`; the lease is released on every exit.

    None when the lease was refused or could not be taken, or the check under it stopped: the reason is printed and no step ran."""
    if mode != "store" or task is None:
        return walk(None)
    holder = chair.lease_holder(_holder_label(a), os.getpid(), socket.gethostname())

    held = [0]  # the epoch the lease is held at now: a re-acquire after an expiry moves it, and the release must follow

    def renew() -> store_cli.LeaseResult:
        return store_cli.lease_renew(runs_dir, store_cli.land_lease_name(task), holder, held[0], _LAND_LEASE_TTL_SECONDS)

    def reacquire() -> store_cli.LeaseResult:
        again = store_cli.lease_acquire(runs_dir, store_cli.land_lease_name(task), holder, _LAND_LEASE_TTL_SECONDS)
        if isinstance(again, store_cli.LeaseGranted):
            held[0] = again.epoch
        return again

    refused_by: list[str | None] = []  # holders the first take was refused by; a body that runs after one ran on a takeover

    def acquire(runs_dir: Path, name: str, holder: str, ttl: int, steal: bool = False) -> store_cli.LeaseResult:
        got = store_cli.lease_acquire(runs_dir, name, holder, ttl, steal=True) if steal else store_cli.lease_acquire(runs_dir, name, holder, ttl)
        if isinstance(got, store_cli.LeaseRefused) and not steal:
            refused_by.append(got.holder)
        return got

    def body(epoch: int) -> tuple[int, list[str], str] | str:
        held[0] = epoch
        if refused_by:  # printed here so it lands before the walk's own output and survives a walk that raises
            print(f"land: taking over the land lease for {task} from {refused_by[0]} (pid gone)")
        stop = recheck()
        guard = _LeaseGuard(f"land lease for {task}", renew, reacquire, time.monotonic, _LAND_LEASE_TTL_SECONDS,
                            lambda kind, result, _renewed, _now: _task_lease_stop(task, kind, result), only=_GUARDED_STEPS)
        return stop if stop is not None else walk(guard)

    ran = land_lease.run_under_land_lease(runs_dir, task, holder, _LAND_LEASE_TTL_SECONDS, body, acquire=acquire,
                                          epoch_now=lambda: held[0], this_host=socket.gethostname(), alive=land_repo_lease.pid_alive)
    if isinstance(ran, land_lease.Refused):
        if ran.holder is None:  # a steal over a dead holder was granted at an unexpected epoch: a live land had taken it first
            print(f"land: refusing, {task} was taken by a live land while taking over a dead holder's lease (refused at epoch {ran.epoch})")
        else:
            print(f"land: refusing, {task} is being landed by {ran.holder} (lease epoch {ran.epoch})")
        return None
    if isinstance(ran, land_lease.Unavailable):
        print(f"land: refusing, could not take the land lease for {task}: {ran.detail}")
        return None
    if ran.warning:
        print(ran.warning)
    if isinstance(ran.value, str):
        print(ran.value)
        return None
    return ran.value


def _land_done_before_fetch(runs_dir: Path, task: str | None, mode: str) -> bool:
    """Edge. True when work_state is store and the store already says `task` is done, so a fetch is pointless."""
    if task is None or mode != "store":
        return False
    initiative = _initiative_of(runs_dir.parent / "work", task)
    return initiative is not None and run_store.task_state_of(runs_dir, initiative, task) == "done"


def _cartridge_checks(repo: Path, profile: dict) -> tuple[list[dict], str | None]:
    """Edge. The profile's team cartridge `landing_areas.checks`, resolved as the chair resolves it, and the error text
    when it cannot be: a profile naming no team and no cartridges_dir has no cartridge, which is `([], None)`."""
    landing, error = _cartridge_landing(repo, profile)
    return list(landing.get("checks") or []), error


def _cartridge_landing(repo: Path, profile: dict) -> tuple[dict, str | None]:
    """Edge. The resolved team cartridge's `landing_areas` and the error text when it cannot be resolved; `({}, None)` when the profile names no cartridge."""
    team, cartridges_dir = profile.get("team"), profile.get("cartridges_dir")
    if not team or not cartridges_dir:
        return {}, None
    try:
        from harness.resolve import overlay_path, resolve_cartridge
        overlay_file = Path(overlay_path(str(repo)))
        overlay = yaml.safe_load(overlay_file.read_text(encoding="utf-8")) if overlay_file.is_file() else None
        cartridge, _ = resolve_cartridge(team, cartridges_dir=Path(cartridges_dir).expanduser(),
                                         skills_root=[Path(r).expanduser() for r in profile.get("skills_roots") or []], overlay=overlay)
    except Exception as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    return dict(cartridge.get("landing_areas") or {}), None


def _required_checks(repo: Path, default_branch: str | None, forge_module) -> tuple[str, ...] | None:
    """Edge. The default branch's required check names, or None when the forge is unnamed, has no such read (the local
    forge), no branch is given, or the read fails; a failure's cause goes to stderr, since the plan only says unreadable."""
    read = getattr(forge_module, "read_required_for_base", None)
    if not default_branch or read is None:
        return None
    try:
        return read(repo, default_branch)
    except Exception as exc:
        print(f"land: required checks unreadable ({type(exc).__name__}: {exc})", file=sys.stderr)
        return None


def _land_repo_facts(repo: Path, profile: dict, default_branch: str | None = None, forge_module=None) -> dict:
    """Edge. What `land.checks_argv` needs: the cartridge's checks plus the repo's `.agent-checks` (parsed by
    `harness.checks.repo_checks`, as the chair does), the files that say whether pytest belongs here, and
    `cartridge_error`, set when the cartridge could not be resolved and the checks are therefore short.
    `land_checks` is the cartridge's; only `ci` reads the forge, for `required_checks`."""
    from harness.check_gate import gate_checks
    from harness.checks import repo_checks
    try:
        agent_checks = (repo / ".agent-checks").read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        agent_checks = ""
    landing, cartridge_error = _cartridge_landing(repo, profile)
    cartridge_checks = list(landing.get("checks") or [])
    facts = {"venv_python": (repo / ".venv" / "bin" / "python").exists(), "uv_lock": (repo / "uv.lock").exists(),
             "python_project": any((repo / name).exists() for name in land.PYTHON_PROJECT_FILES),
             "checks": land.resolved_checks(cartridge_checks, repo_checks(agent_checks)), "cartridge_error": cartridge_error,
             "ticket_checks": gate_checks(landing, "ticket")}
    land_checks = landing.get("land_checks")
    if land_checks is None:
        return facts
    ci = {"required_checks": _required_checks(repo, default_branch, forge_module)} if land_checks == "ci" else {}
    return {**facts, "land_checks": land_checks, **ci}


def _land_checks_lines(steps: list[dict]) -> list[str]:
    """The dry run's lines, one per checks step and labelled by what it checks, each check as its command (or
    `name = command` when the name is neither the command nor its first word); `none declared` for a step holding none.
    A skipped step says `skipped (<reason>)`; a kept step's line is followed by its reason."""
    def show(name: str, line: str) -> str:
        return line if name == line or line.split(None, 1)[:1] == [name] else f"{name} = {line}"
    lines = []
    for s in (s for s in steps if s["kind"] == "checks"):
        gate = "ticket gate" if s.get("gate") == "ticket" else "checks"
        label = f"{gate} ({s.get('worktree_of') or s.get('branch') or 'repo'}): "
        if "skipped" in s:
            lines.append(f"{label}skipped ({s['skipped']})")
            continue
        lines.append(label + ("; ".join(show(n, line) for n, line in s["checks"]) or "none declared"))
        if "kept" in s:
            lines.append(f"  {s['kept']}")
    return lines


def _base_behind_warning(runs_dir: Path, run_id: str, repo: Path, default_branch: str, limit: int) -> str | None:
    """Edge. `land.behind_warning` for the base commit the run's remote record holds; a local run or an older record has none."""
    path = remote_lane.remote_record_path(runs_dir, run_id)
    record = remote_lane.parse_remote_record(path.read_text()) if path.exists() else None
    base_sha = None if record is None else record.get("base_sha")
    if base_sha is None:
        return None
    count = _git_out(repo, "rev-list", "--count", f"{base_sha}..{default_branch}")
    return land.behind_warning(base_sha, int(count) if count and count.isdigit() else None, limit, default_branch)


def _print_behind_warning(a: argparse.Namespace, runs_dir: Path, run_id: str, repo: Path, default_branch: str) -> None:
    behind = _base_behind_warning(runs_dir, run_id, repo, default_branch, getattr(a, "behind_limit", 20))
    if behind is not None:
        print(behind, file=sys.stderr)


def _runs_land(a: argparse.Namespace) -> int:
    repo = Path(a.repo).expanduser()
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"land: {reason}")
        return 2
    run_id = _resolved_run_id(runs_dir, a.run_id)
    if a.task and _land_done_before_fetch(runs_dir, a.task, work_state.work_state_mode(_lake_provider(a)[0])):
        print(_land_approval_stop("store", "done", None))
        return 3
    # A remote run fetched before the marker existed has none, so land refuses with the fetch hint; a re-fetch is
    # idempotent (rsync -a and git fetch of the same refs).
    fetched = remote_lane.fetched_record_path(runs_dir, run_id).exists()
    if remote_lane.land_needs_fetch(remote_lane.remote_record_path(runs_dir, run_id).exists(), fetched):
        print(f"land: {run_id} is a remote run; run cox runs fetch {run_id}, then cox runs land {run_id} again")
        return 2
    if a.apply:
        guard_rc = _leader_guard_or_refuse(runs_dir, _holder_label(a), a.force, claim=not a.no_claim, explicit_label=getattr(a, "label", None))
        if guard_rc is not None:
            return guard_rc
    profile_text = _read_text_or_none(_profile_path(a))
    try:
        profile = route.parse_profile(profile_text) if profile_text is not None else {}
    except route.ProfileError as exc:
        print(f"land: profile unreadable: {exc}")
        return 2
    checked = land_checkout_guard.verdict(
        _repo_is_detached(repo),
        land_checkout_guard.is_install_managed(str(repo), _install_roots(profile)),
        repo_clone.working_clone(profile),
    )
    if checked.refusal:
        print(f"land: refusing, {checked.refusal}")
        return 2
    if checked.notice:
        print(f"land: {checked.notice}")
    base_flag = getattr(a, "base", None)
    if not base_flag:
        phase_of_land = getattr(a, "phase", None) or (None if a.task else _phase_needing_land(runs_dir, run_id))
        base_flag, refusal = _initiative_target_branch(profile, repo, runs_dir.parent, _land_initiative(runs_dir, run_id, phase_of_land, a.task))
        if refusal:
            print(f"land: refusing, {refusal}")
            return 2
    try:
        default_branch = base_branch.base_ref(str(repo), base_flag).removeprefix("origin/")
    except base_branch.BaseError as exc:
        print(f"land: refusing, {exc}")
        return 2
    repo_facts = _land_repo_facts(repo, profile, default_branch, forge.forge_for(forge.forge_name(profile)))
    if repo_facts["cartridge_error"]:
        # The chair fails a run whose cartridge will not resolve; a land that went on would push after fewer checks than the build ran.
        shortfall = f"cartridge checks unavailable ({repo_facts['cartridge_error']}); the land would run fewer checks than the build did"
        if a.apply:
            print(f"land: refusing, {shortfall}")
            return 2
        print(f"land: {shortfall}; the list below is short", file=sys.stderr)
    phase = getattr(a, "phase", None) or (None if a.task else _phase_needing_land(runs_dir, run_id))
    # No phase named and records in several phases: the ticket's last phase, but only to finish an already merged PR (checked below).
    inferred = phase is None and not a.task and _land_record(runs_dir, run_id, None)[2] > 1
    phase = _last_phase_with_manifest(runs_dir, run_id) if inferred else phase
    inferred = inferred and phase is not None
    land_mode = work_state.work_state_mode(_lake_provider(a)[0]) if a.apply else "files"
    record, item_path, lease_task = None, None, None
    land_at, ticket_tasks = None, []
    merged_pr: dict | None = None
    if phase:
        if land_mode == "store":
            _backfill_phase_task_files(runs_dir, run_id, phase)
        phase_record, task_records, task_paths, searched = _land_phase_record(runs_dir, run_id, phase)
        if phase_record is None:
            print(f"land: no phase record at {searched}")
            return 2
        initiative = phase_record.get("initiative") or _initiative_of_phase(runs_dir.parent / "work", phase, run_id)
        items, items_path = (_phase_items(runs_dir.parent / "work", initiative, phase, runs_dir=runs_dir, land_mode=land_mode)
                              if initiative else (None, f"{runs_dir.parent / 'work'} (no single initiative holds phase {phase})"))
        if items is None:
            print(f"land: no work items at {items_path}, expected the phase's tickets")
            return 2
        initiative_title = _initiative_title(runs_dir.parent / "work", initiative)
        # An all-done phase is landed and its branch was deleted on purpose; only an unfinished phase needs the branch.
        missing_on = (None if all(i["status"] == "done" for i in items)
                      else _land_refetch_phase_branch(a, runs_dir, run_id, repo, f"epic/{initiative}/{phase}"))
        if missing_on is not None:
            print(f"land: stopped at fetch_phase_branch: phase branch not fetched from {missing_on}")
            return 2
        phase_pr, pr_forge = None, forge.forge_for(forge.forge_name(profile)) if a.apply else None
        if pr_forge is not None:
            phase_pr = _phase_pr_facts(repo, f"pr/{initiative}--{phase}", pr_forge)
            if isinstance(phase_pr, str):
                print(f"land: refusing, {phase_pr}")
                return 2
        read_forge = pr_forge or forge.forge_for(forge.forge_name(profile))
        merged_pr = (_phase_merged_pr(repo, initiative, phase, default_branch, read_forge, _carried_files(items, task_records),
                                      _parent_phase(runs_dir.parent / "work", initiative, phase))
                     if read_forge is not None else None)
        already_merged = merged_pr is not None
        if inferred and not already_merged:
            _, searched, count = _land_record(runs_dir, run_id, None)
            print(f"land: looked in {searched}, found {count} task records, expected 1")
            return 2
        land_at = _phase_land_at(phase_record, profile, runs_dir.parent / "work", initiative)
        ticket_items, phases = _ticket_items(runs_dir.parent / "work", initiative, runs_dir, land_mode, (phase, items)) if land.is_initiative(land_at) else ({}, [])
        ticket_tasks = list(ticket_items)
        # An already merged phase puts nothing new on the target branch; a ticket land carries the earlier phases in its one PR.
        parent_wait = (None if already_merged else _parent_land_refusal(
            [i["id"] for i in items], _initiative_land_facts(runs_dir.parent / "work", initiative, runs_dir, land_mode), ticket_tasks))
        if parent_wait:
            return _land_parent_refused(a, parent_wait)
        for earlier_phase in (phases if land_mode == "store" else []):
            if earlier_phase != phase:
                _backfill_phase_task_files(runs_dir, run_id, earlier_phase)
        earlier = [r for p in phases if p != phase for r in _land_phase_record(runs_dir, run_id, p)[1]
                   if r["task"] in ticket_items and r.get("status", "approved") == "approved"]
        ticket_present = _ticket_present(repo, phase_record.get("card_branch") or f"epic/{initiative}/{phase}", earlier)
        # Approved in the store with no task record in any phase, even after the earlier-run fallback: built elsewhere.
        recorded = {r["task"] for p in phases for r in _land_phase_record(runs_dir, run_id, p)[1]} | {r["task"] for r in task_records}
        ticket_unrecorded = sorted(t for t in (ticket_items if land.is_initiative(land_at) else _phase_task_items(items)) if t not in recorded)
        from core.landing import phase_land_deferred  # lazy: core sits beside agent_tools in one venv
        plan_steps = land.land_plan(
            {**phase_record, "initiative": initiative, "initiative_title": initiative_title,
             "last_phase": not phase_land_deferred(land_at, phase, phases) if phases else bool(phase_record.get("last_phase"))},
            {}, default_branch,
            repo_facts, items=items, task_records=task_records, phase_pr=phase_pr,
            task_facts=_phase_task_facts(repo, f"epic/{initiative}/{phase}", items, task_records),
            already_merged=already_merged, ticket_tasks=ticket_tasks, ticket_present=ticket_present,
            ticket_records=[r for r in earlier if r["task"] in ticket_present], ticket_unrecorded=ticket_unrecorded,
        )
        if inferred:  # a re-run named by no phase, after the PR merged, leaves a task already done (by hand or by a first run) alone
            finished = {i["id"] for i in items if i["status"] == "done"}
            plan_steps = [s for s in plan_steps if not (s["kind"] == "mark_done" and s["task"] in finished)]
        earlier_paths = {t: p for ph in phases if ph != phase for t, p in _land_phase_record(runs_dir, run_id, ph)[2].items()}
        steps = _land_enrich(plan_steps, path=searched, worktree_root=a.worktree_root, task_paths={**earlier_paths, **task_paths},
                              umbrella=profile.get("umbrella_dir"), store_runs_dir=str(runs_dir),
                              task_items={**_phase_task_items(items), **ticket_items}, check_paths=True)
        lease_task = f"phase:{initiative}/{phase}"
    else:
        record, searched, count, source = _land_load(runs_dir, run_id, a.task)
        if record is None:
            print(f"land: looked in {searched}, found {count} task records, expected 1")
            return 2
        print(f"land: record from {source}", file=sys.stderr)
        parent_wait = (_parent_land_refusal([record["task"]], _initiative_land_facts(runs_dir.parent / "work", record["initiative"], runs_dir, land_mode))
                       if record.get("initiative") and record.get("phase") else None)
        if parent_wait:
            return _land_parent_refused(a, parent_wait)
        lease_task = record["task"]
        branches = _land_branches(repo, record, default_branch)
        item_path = (str(runs_dir.parent / "work" / record["initiative"] / record["phase"] / f"{record['task']}.md")
                     if record.get("initiative") else None)
        item_id, issue = _land_item_facts(item_path)
        sync = (
            argparse.Namespace(
                item=None,
                workspace=str(runs_dir.parent),
                dry_run=False,
                project=None,
                profile=str(_profile_path(a)),
                runs_dir=str(runs_dir),
            )
            if a.apply
            else None
        )
        scope, reference = _land_reference(profile, str(runs_dir.parent), record, item_id, issue, repo, sync)
        plan_steps = land.land_plan(record, branches, default_branch, repo_facts,
                                    tracker=_resolved_tracker(profile, runs_dir), issue=issue,
                                    scope=scope, reference=reference,
                                    phase_done=_land_phase_done(runs_dir, run_id, record),
                                    compared=_land_branch_counts(repo, record, default_branch, branches))
        steps = _land_enrich(plan_steps, path=searched, worktree_root=a.worktree_root, item_path=item_path,
                              workspace=str(runs_dir.parent), item_id=item_id,
                              profile=str(_profile_path(a)), runs_dir=str(runs_dir), umbrella=profile.get("umbrella_dir"))
    level = a.gate or _resolved_gate_level(runs_dir)
    if a.gate:
        print(f"land: --gate {level} overrides the resolved level")
    planned, steps = steps, (land.gate_steps(steps, level, land_at, ticket_tasks) if land_at else land.gate_steps(steps, level))
    level = land.gate_level(level, land_at)
    if not a.apply:
        _print_behind_warning(a, runs_dir, run_id, repo, default_branch)
        for line in _land_checks_lines(steps):
            print(line, file=sys.stderr)
        print(json.dumps(steps, indent=2))
        return 2 if any(s["kind"] == "refuse" for s in steps) else 0
    steps = _with_check_cap(steps, os.cpu_count() or 1, _check_capacity(runs_dir, profile, socket.gethostname()))
    forge_choice = forge.forge_name(profile)
    forge_module = forge.forge_for(forge_choice)
    if forge_module is None:
        print(f"land: no forge named {forge_choice} (built in: local, github, auto)")
        return 2
    stale = forge.missing_refs(forge_module)
    if stale:
        print(f"land: refusing, forge {forge_choice} does not accept {', '.join(stale)}; see agent_tools/forge.py")
        return 2
    print(f"forge: {forge_choice}")
    if land_mode == "store" and record is not None:
        stop = _land_store_stop(runs_dir, record, item_path)
        if stop is not None:
            print(stop)
            return 2
    if _repo_is_dirty(repo):
        print(f"land: refusing, {repo} is dirty")
        return 2
    sync, sync_detail = _sync_default_branch(repo, default_branch)
    if sync == "fast_forward":
        print(f"land: {sync_detail}")
    elif sync == "refuse":
        print(f"land: refusing, {sync_detail}")
        return 2
    cherry_pick, rebuild, recreate = next((s for s in steps if s["kind"] == "cherry_pick"), None), None, None
    _print_behind_warning(a, runs_dir, run_id, repo, default_branch)
    if cherry_pick is not None:
        # An existing pr/<task> is not necessarily stale or foreign: a retried
        # push can leave a same-tree branch behind, and the rerun should reuse it.
        decision = _land_leftover(repo, cherry_pick, _land_resume(repo, cherry_pick, forge_module), forge_module)
        rebuild = decision if "backup_refs" in decision else None
        recreate = decision if "old_tip" in decision else None
        if decision["kind"] == "refuse":
            print(f"land: refusing, branch {cherry_pick['onto']} already exists in {repo}: {decision['reason']}")
            return 2
        if decision["kind"] == "resume":
            print(f"land: resuming on existing branch {cherry_pick['onto']}")
        if decision["kind"] == "adopt":
            print(f"land: adopting open pull request #{decision['pr']} on {cherry_pick['onto']}")
        steps = land.resume_steps(steps, decision, cherry_pick["onto"])
        planned = land.resume_steps(planned, decision, cherry_pick["onto"])
    if merged_pr is not None:
        print(_merged_line(merged_pr))
    # Steps start here: every return from now on is logged. Earlier returns are not.
    repo_lost: list[str] = []
    stops: list[dict] = []

    repo_epoch = [0]  # the epoch the repo lease is held at now: a re-acquire after an expiry moves it, and the release must follow

    repo_slot = [0]  # the slot of the repo lease this land holds: it keeps the same one through a re-acquire

    def reacquire_repo() -> store_cli.LeaseResult:
        again = land_repo_lease.acquire_slot(runs_dir, str(repo), repo_slot[0], repo_holder, _REPO_LEASE_TTL_SECONDS)
        if isinstance(again, store_cli.LeaseGranted):
            repo_epoch[0] = again.epoch
        return again

    def walk(task_guard):
        repo_guard = _repo_lease_guard(
            str(repo),
            lambda: land_repo_lease.renew_slot(runs_dir, str(repo), repo_slot[0], repo_holder, repo_epoch[0], _REPO_LEASE_TTL_SECONDS),
            time.monotonic, _REPO_LEASE_TTL_SECONDS, repo_lost, reacquire_repo) if isinstance(got, store_cli.LeaseGranted) else None
        keep = [g for g in (repo_guard, task_guard) if isinstance(g, _LeaseGuard)]
        # The repo lease is checked first: a lost repo lease stops the walk before the task lease is renewed.
        guard = task_guard if repo_guard is None else _first_stop(repo_guard, task_guard)
        # The leftover rebuild deletes a branch, so it runs only once the repo lease is held: a concurrent
        # land mid-walk owns that branch, and the lease is what refuses this one before it touches it.
        failed = ("no repo lease to rebuild under" if not leased else _land_rebuild(repo, cherry_pick["onto"], rebuild)) if rebuild else None
        if failed:
            print(f"land: refusing, branch {cherry_pick['onto']} already exists in {repo}: {rebuild['reason']}")
            print(f"land: not rebuilt: {failed}")
            return 2, [], ""
        if rebuild:
            saved = " and ".join(f"{tip[:8]} to {ref}" for ref, tip in rebuild["backup_refs"].items())
            print(f"land: backed up {saved}, rebuilding {cherry_pick['onto']}")
        # An empty leftover holds nothing to back up; its local branch is deleted under the same lease.
        dropped = ("no repo lease to recreate under" if not leased
                   else _land_recreate(repo, cherry_pick["onto"], recreate["old_tip"])) if recreate else None
        if dropped:
            print(f"land: refusing, branch {cherry_pick['onto']} already exists in {repo}: {recreate['reason']}")
            print(f"land: not recreated: {dropped}")
            return 2, [], ""
        if recreate:
            print(f"land: {cherry_pick['onto']} at {recreate['old_tip'][:8]} holds nothing beyond {cherry_pick['from']}, "
                  f"recreating it")
        return _land_execute(repo, steps, planned, record, item_path, level, a.no_merge, forge_module, by=_holder_label(a),
                             mode=land_mode, guard=guard, stops=stops, keep=keep, pr=_merged_pr_ref(merged_pr),
                             lock=_merge_lock_factory(runs_dir, str(repo), repo_holder, repo_limit, leased))

    repo_holder = f"{_holder_label(a)}@{socket.gethostname()}:{os.getpid()}"
    repo_limit = land_limit.repo_land_limit(str(repo))
    repo_slot[0], got = land_repo_lease.acquire_in_slots(
        runs_dir, str(repo), repo_holder, _REPO_LEASE_TTL_SECONDS, socket.gethostname(), repo_limit,
        alive=land_repo_lease.pid_alive)
    if getattr(a, "wait_lock", False) and isinstance(got, store_cli.LeaseRefused):
        print(land_repo_lease.waiting_message(str(repo), got.holder))
        repo_slot[0], got = land_repo_lease.acquire_in_slots_waiting(
            runs_dir, str(repo), repo_holder, _REPO_LEASE_TTL_SECONDS, socket.gethostname(), repo_limit,
            getattr(a, "wait_lock_timeout", 600), _LAND_LOCK_POLL_SECONDS, alive=land_repo_lease.pid_alive)
    leased = isinstance(got, store_cli.LeaseGranted)
    if isinstance(got, store_cli.LeaseGranted):
        repo_epoch[0] = got.epoch
    if isinstance(got, store_cli.LeaseRefused):
        print(land_repo_lease.refusal_message(str(repo), got.holder or "another land"))
        walked = None
    elif isinstance(got, store_cli.LeaseGranted):
        try:
            walked = _land_walked(a, runs_dir, land_mode, lease_task,
                                   lambda: _land_store_stop(runs_dir, record, item_path) if record else None, walk)
        finally:
            if not repo_lost:  # a refused renew means another land holds it now; releasing would not be ours to do
                # Release goes through the store, so a refused store can fail it too; that is warned, never raised
                # over the walk's own error, and the TTL plus the dead-pid takeover clear the lease for the next land.
                name = land_repo_lease.slot_lease_name(str(repo), repo_slot[0])
                try:
                    freed = land_repo_lease.release_slot(runs_dir, str(repo), repo_slot[0], repo_holder, repo_epoch[0])
                    why = freed.detail if isinstance(freed, store_cli.LeaseError) else (
                        "the store is not available" if isinstance(freed, store_cli.NotAvailable) else None)
                except Exception as exc:
                    why = f"{type(exc).__name__}: {exc}"
                if why is not None:
                    print(f"warning: repo lease {name} not released: {why}", file=sys.stderr)
    else:
        # A store outage (NotAvailable/LeaseError) must not block a land: walk without the repo lease.
        walked = _land_walked(a, runs_dir, land_mode, lease_task,
                               lambda: _land_store_stop(runs_dir, record, item_path) if record else None, walk)
    rc, reached, pr = walked if walked is not None else (2, [], "")  # a refused lease stops before any step, and is logged too
    task = record["task"] if record else None
    _mark_stopped_land(repo, runs_dir, rc, record, item_path, stops, cherry_pick, _holder_label(a), land_mode)
    _append_land_log(runs_dir, land.land_log_row(datetime.datetime.now(datetime.UTC).isoformat(), run_id, task, reached, rc, pr,
                                                   stops[0] if stops else None))
    _lake_after_land(a, runs_dir, rc, reached)
    return rc


_BATCH_POLL_S = 30.0  # how often a queue pass re-reads the sources while it waits for a full batch


class _BatchLease:
    """Edge. The repo land lease for one queue pass, through the same acquire, renew and release calls `_runs_land` makes.

    Taken on the first batch to open, never while sources are collected, so a normal land is not blocked meanwhile."""

    def __init__(self, runs_dir: Path, repo: Path, holder: str):
        self.runs_dir, self.repo, self.holder = runs_dir, repo, holder
        self.slot, self.epoch, self.held, self.busy = 0, 0, False, None
        self.lost: list[str] = []
        self.limit = land_limit.repo_land_limit(str(repo))

    def take(self) -> str | None:
        """The refusal text when another land holds the repo, else None. A store outage lands without the lease."""
        if self.busy is not None or self.held:
            return self.busy
        self.slot, got = land_repo_lease.acquire_in_slots(
            self.runs_dir, str(self.repo), self.holder, _REPO_LEASE_TTL_SECONDS, socket.gethostname(), self.limit)
        if isinstance(got, store_cli.LeaseRefused):
            self.busy = land_repo_lease.refusal_message(str(self.repo), got.holder or "another land")
        elif isinstance(got, store_cli.LeaseGranted):
            self.held, self.epoch = True, got.epoch
        return self.busy

    def _reacquire(self) -> store_cli.LeaseResult:
        again = land_repo_lease.acquire_slot(self.runs_dir, str(self.repo), self.slot, self.holder, _REPO_LEASE_TTL_SECONDS)
        if isinstance(again, store_cli.LeaseGranted):
            self.epoch = again.epoch
        return again

    def guard(self) -> _LeaseGuard | None:
        if not self.held:
            return None
        return _repo_lease_guard(
            str(self.repo),
            lambda: land_repo_lease.renew_slot(self.runs_dir, str(self.repo), self.slot, self.holder, self.epoch, _REPO_LEASE_TTL_SECONDS),
            time.monotonic, _REPO_LEASE_TTL_SECONDS, self.lost, self._reacquire)

    def merge_lock(self) -> Callable[[], contextlib.AbstractContextManager] | None:
        return _merge_lock_factory(self.runs_dir, str(self.repo), self.holder, self.limit, self.held)

    def release(self) -> None:
        if not self.held or self.lost:  # a refused renew means another land holds it now; releasing is not ours to do
            return
        name = land_repo_lease.slot_lease_name(str(self.repo), self.slot)
        try:
            freed = land_repo_lease.release_slot(self.runs_dir, str(self.repo), self.slot, self.holder, self.epoch)
            why = freed.detail if isinstance(freed, store_cli.LeaseError) else (
                "the store is not available" if isinstance(freed, store_cli.NotAvailable) else None)
        except Exception as exc:
            why = f"{type(exc).__name__}: {exc}"
        if why is not None:
            print(f"warning: repo lease {name} not released: {why}", file=sys.stderr)


def _ready_phase_ids(runs_dir: Path, work_root: Path) -> list[tuple[str, str, str]]:
    """Edge. `(initiative, phase, run)` for every phase directory under `runs/<run>/tasks/` that a work initiative owns."""
    found = [(_initiative_of_phase(work_root, p.name, p.parent.parent.name), p.name, p.parent.parent.name)
             for p in sorted(runs_dir.glob("*/tasks/*")) if p.is_dir()]
    return [(i, ph, r) for i, ph, r in found if i]


def _batch_sources(runs_dir: Path, repo: Path, land_mode: str, wanted: list[tuple[str, str, str]] | None) -> list[dict]:
    """Edge. The source mappings of the ready phases among `wanted`, or among every phase when None.

    Reads the records `_runs_land` reads for a phase. A phase with no tickets, no record, or a task still unapproved is skipped."""
    work_root = runs_dir.parent / "work"
    sources = []
    for initiative, phase, run in wanted if wanted is not None else _ready_phase_ids(runs_dir, work_root):
        items, _ = _phase_items(work_root, initiative, phase, runs_dir=runs_dir, land_mode=land_mode)
        if not items or not land_batch_effects.phase_ready(items):
            continue
        if land_mode == "store":
            _backfill_phase_task_files(runs_dir, run, phase)
        record, task_records, task_paths, _ = _land_phase_record(runs_dir, run, phase)
        if record is not None:
            sources.append(land_batch_effects.build_source(initiative, phase, run, str(repo), items, task_records, task_paths))
    return sources


def _batch_effects(a: argparse.Namespace, repo: Path, runs_dir: Path, profile: dict, lease: _BatchLease,
                   wanted: list[tuple[str, str, str]] | None):
    """Edge. The queue's `Effects`, or the reason they cannot be built. Its steps run through `_land_execute`, as a land's do."""
    try:
        default_branch = base_branch.base_ref(str(repo), getattr(a, "base", None)).removeprefix("origin/")
    except base_branch.BaseError as exc:
        return f"land: refusing, {exc}"
    forge_choice = forge.forge_name(profile)
    forge_module = forge.forge_for(forge_choice)
    if forge_module is None:
        return f"land: no forge named {forge_choice} (built in: local, github, auto)"
    stale = forge.missing_refs(forge_module)
    if stale:
        return f"land: refusing, forge {forge_choice} does not accept {', '.join(stale)}; see agent_tools/forge.py"
    repo_facts = _land_repo_facts(repo, profile, default_branch, forge_module)
    if repo_facts["cartridge_error"] and a.apply:
        return f"land: refusing, cartridge checks unavailable ({repo_facts['cartridge_error']}); the land would run fewer checks than the build did"
    land_mode = work_state.work_state_mode(_lake_provider(a)[0]) if a.apply else "files"
    level = a.gate or _resolved_gate_level(runs_dir)
    capacity = _check_capacity(runs_dir, profile, socket.gethostname())

    def run_steps(steps: list[dict], batch: list[dict]) -> tuple[int, list[str], str]:
        enriched = _with_check_cap(
            _land_enrich(steps, path="", worktree_root=a.worktree_root, workspace=str(runs_dir.parent),
                         profile=str(_profile_path(a)), runs_dir=str(runs_dir), umbrella=profile.get("umbrella_dir"),
                         task_paths={t: p for s in batch for t, p in s["task_paths"].items()},
                         task_items={t: f for s in batch for t, f in s["task_items"].items()}),
            os.cpu_count() or 1, capacity)
        repo_guard = lease.guard()
        return _land_execute(repo, enriched, steps, None, None, level, False, forge_module, by=_holder_label(a), mode=land_mode,
                             guard=_first_stop(repo_guard, None) if repo_guard else None, stops=[],
                             keep=[repo_guard] if repo_guard else [], lock=lease.merge_lock())

    return land_batch_effects.make_effects(
        lambda: _batch_sources(runs_dir, repo, land_mode, wanted), run_steps, repo_facts, default_branch,
        lambda: datetime.datetime.now(datetime.UTC).strftime("%Y%m%d%H%M%S"))


def _runs_land_batch(a: argparse.Namespace) -> int:
    """Edge. One merge-queue pass: collect, take the repo lease, open the first batch, print the result.

    Exits 2 when the lease is refused, 1 when every opened source was returned, else 0. A returned source keeps its
    tasks approved and its phase branch untouched."""
    repo = Path(a.repo).expanduser()
    wanted, bad = land_batch_effects.parse_source_ids(a.source or [])
    if bad:
        print(f"land-batch: {bad}")
        return 2
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"land-batch: {reason}")
        return 2
    if a.apply:
        guard_rc = _leader_guard_or_refuse(runs_dir, _holder_label(a), a.force, claim=not a.no_claim, explicit_label=getattr(a, "label", None))
        if guard_rc is not None:
            return guard_rc
    profile_text = _read_text_or_none(_profile_path(a))
    try:
        profile = route.parse_profile(profile_text) if profile_text is not None else {}
    except route.ProfileError as exc:
        print(f"land-batch: profile unreadable: {exc}")
        return 2
    configured = land_batch_settings.merge_queue_setting(profile)
    settings = land_batch_settings.MergeQueue(a.batch_size or configured.batch_size, configured.wait_s if a.wait_s is None else a.wait_s)
    lease = _BatchLease(runs_dir, repo, f"{_holder_label(a)}@{socket.gethostname()}:{os.getpid()}")
    effects = _batch_effects(a, repo, runs_dir, profile, lease, wanted)
    if isinstance(effects, str):
        print(effects)
        return 2
    if not a.apply:
        sources = effects.read_sources()
        batch = land_batch_group.disjoint_batches(sources, settings.batch_size)[0] if sources else []
        print(json.dumps({"would_batch": [f"{s['initiative']}/{s['phase']}" for s in batch],
                          "ready": [f"{s['initiative']}/{s['phase']}" for s in sources]}, indent=2))
        return 0

    def land(group: list[dict]):
        busy = lease.take()
        return land_batch_run.Outcome(busy, None) if busy is not None else effects.land(group)

    try:
        result = land_batch_run.run_queue(settings, land_batch_run.Effects(effects.read_sources, land, effects.sleep, effects.now), _BATCH_POLL_S)
    finally:
        lease.release()
    if lease.busy is not None:
        print(lease.busy)
        return 2
    print(land_batch_run.render_lines(result), end="")
    return 1 if result.returned and not result.landed else 0


def _mark_land_conflict(runs_dir: Path, record: dict, item_path: str | None, reason: str, by: str, mode: str) -> None:
    """Edge. A land whose cherry-pick conflicted leaves the task `ready`, not `approved`, with `reason` as a `land_conflict`
    marker and a body note, on the store row and the item file. The row is written first, so a crash before the state
    move leaves an approved task with the marker, which `route edit --rebuild` accepts."""
    initiative, task = record.get("initiative"), record.get("task")
    if not initiative or not task:
        return
    stored = next((r for r in run_store.read_queue(runs_dir, initiative, kind="task") if r.get("task_id") == task), None)
    key = {"initiative": initiative, "task_id": task}
    failed = "no store row"
    if stored is not None:
        marked = {"extra": {**(stored.get("extra") or {}), land.LAND_CONFLICT_KEY: reason},
                  "body": route_edit.append_note(stored.get("body") or "", land.conflict_note(reason))}
        failed = run_store.upsert_row_detail(runs_dir, {**key, **marked})
        if failed:
            print(f"warning: the store did not take the land conflict of {task}: {failed}")
    if mode == "store":
        stop = land.set_state_stop(store_cli.set_state(runs_dir, initiative, task, "ready", by, expected="approved"))
        if stop is not None:
            print(stop)
            if stored is not None and not failed:  # the task is not ours to mark: put the row back as it was
                run_store.upsert_row_detail(runs_dir, {**key, "extra": stored.get("extra") or {}, "body": stored.get("body") or ""})
            return
    item = Path(item_path) if item_path else None
    if item is not None and item.exists():
        new_text = land.conflict_to_ready(item.read_text(encoding="utf-8"), reason)
        if new_text is not None:
            item.write_text(new_text, encoding="utf-8")
            queue_export.record_written_paths(runs_dir.parent, [item.absolute()])
    if mode != "store":
        line = store_cli.mirror_state(runs_dir, initiative, task, "ready", by)
        if line is not None:
            print(line)
    print(f"land: {task} is ready again, to be rebuilt: {reason}")


def _mark_stopped_land(repo: Path, runs_dir: Path, rc: int, record: dict | None, item_path: str | None, stops: list[dict],
                       cherry_pick: dict | None, by: str, mode: str) -> None:
    """Edge. After a land that stopped (exit 1) on its cherry-pick conflicting, mark the task for a rebuild."""
    if rc != 1 or record is None or not stops or cherry_pick is None:
        return
    base = f"{cherry_pick['from']}@{(_git_out(repo, 'rev-parse', cherry_pick['from']) or '')[:8]}"
    reason = land.land_conflict_reason(stops[0]["stopped_step"], stops[0]["stop_detail"], base)
    if reason is not None:
        _mark_land_conflict(runs_dir, record, item_path, reason, by, mode)


def _append_land_log(runs_dir: Path, row: dict) -> None:
    jsonl_log.append_line(runs_dir / "land.jsonl", row, sync=True)


def should_sync_lake(rc: int, reached: Sequence[str], extra_imports: bool, config_resolved: bool,
                     catalog_path: Path | None, catalog_exists: bool) -> bool:
    """A land syncs the lake when it exited 0 having reached `mark_done`, the extra imports and the config resolved.

    `catalog_path` is the SQLite catalog file, None for any other catalog; that file must already exist."""
    return (rc == 0 and "mark_done" in reached and extra_imports and config_resolved
            and (catalog_path is None or catalog_exists))


def _lake_after_land(a: argparse.Namespace, runs_dir: Path, rc: int, reached: Sequence[str]) -> None:
    """Edge. Sync a lake that already exists once a land has closed its item. Never changes the land's exit code."""
    try:
        # Imported here, as `_lake_doctor` does: the lake modules stay off cli.py's import path.
        from agent_tools import lake_doctor

        runs = runs_dir.resolve()
        provider, problem = _lake_provider(a)
        config = lake_config.resolve_lake(provider, runs)
        catalog_path = lake_doctor.sqlite_catalog_path(config.catalog_uri)
        extra = all(importlib.util.find_spec(name) is not None for name in ("pyiceberg", "pyarrow"))
        exists = catalog_path is not None and catalog_path.exists()
        if not should_sync_lake(rc, reached, extra, problem is None, catalog_path, exists):
            return
        root = store_url.profile_traces_root(provider, runs).url
        root = root if "://" in root else str(Path(root).resolve())
        sync = _lake_real_run(config, store_url.profile_store_url(provider, runs), root)
        outcome = lake_lease.sync_under_lease(sync, runs)
        if isinstance(outcome, lake_lease.Synced):
            report = outcome.value
            rows = sum(t["rows_appended"] for t in report["tables"])
            print(f"lake: +{rows} rows, +{report['traces']['registered']} trace files")
        elif isinstance(outcome, lake_lease.SkippedHeld):
            print(f"lake: sync skipped (held by {outcome.holder})")
        else:
            print(f"lake: sync skipped ({outcome.name})")
    except Exception as exc:  # the land has already happened; a lake problem must not fail it
        print(f"lake: sync skipped ({type(exc).__name__})")


def _land_execute(repo: Path, steps: list[dict], planned: list[dict], record: dict | None, item_path: str | None,
                  level: str, no_merge: bool, forge_module=forge_github, by: str = _UNLABELED,
                  mode: str = "files", guard: Callable[[str], str | None] | None = None,
                  stops: list[dict] | None = None, keep: Sequence[_LeaseGuard] = (),
                  lock: Callable[[], contextlib.AbstractContextManager] | None = None, pr: str = "") -> tuple[int, list[str], str]:
    """Walk `steps`; return the exit code, the step kinds run in order, and the PR url ("" when none opened).

    `pr` seeds the running PR, so a land that opens none still records the PR that carried its work.

    A pr branch's land worktree is dropped before `merge` and on every stop; the branch itself stays.
    `guard(kind)`, when given, runs before each step; a reason back stops the land with exit 2 before that step.
    Each lease guard in `keep` also renews on a timer for the whole walk, so a step that blocks past the ttl keeps its lease.
    `stops`, when given, receives the land log keys of the step that stopped the land."""
    built: list[str] = []
    try:
        with contextlib.ExitStack() as renewing:
            for kept in keep:
                renewing.enter_context(kept.renewing())
            return _land_walk(repo, steps, planned, record, item_path, level, no_merge, forge_module, built, by=by, mode=mode,
                              guard=guard, stops=stops, lock=lock, pr=pr)
    finally:
        for branch in built:
            _remove_land_worktree(repo, branch)


def _note_stop(repo: Path, step: dict, detail: str, stops: list[dict] | None) -> None:
    """Edge. Print the closing `land: stopped at` line and hand the log keys to `stops`.

    A squash conflict resolves its branch and from refs here; neither moves when the squash is refused."""
    squash = step["kind"] == "squash_phase"
    fields = land.stop_fields(step["kind"], detail, _git_out(repo, "rev-parse", step["branch"]) if squash else None,
                              _git_out(repo, "rev-parse", step["from"]) if squash else None)
    print(f"land: stopped at {step['kind']}: {detail}")
    if stops is not None:
        stops.append(fields)


# The note land.py plans for an earlier phase; the chair reads it from this output to defer, not fail, that land. Change them together.
_TICKET_GRANULARITY_NOTE = "ticket granularity, the one pull request opens at the last phase"


def _land_walk(repo: Path, steps: list[dict], planned: list[dict], record: dict | None, item_path: str | None,
               level: str, no_merge: bool, forge_module, built: list[str], by: str = _UNLABELED,
               mode: str = "files", guard: Callable[[str], str | None] | None = None,
               stops: list[dict] | None = None,
               lock: Callable[[], contextlib.AbstractContextManager] | None = None, pr: str = "") -> tuple[int, list[str], str]:
    landing = ""  # what a landed task's review PRs are closed against; "" until a merge succeeds
    reached: list[str] = []
    looped = False
    pr = pr or _open_pr_ref(repo, steps, forge_module)
    pr_failed: tuple[dict, str] | None = None  # the pr step that failed after the push, and why
    merge_ran = False  # once a merge step succeeded, a later step's exception is a failed result with the tasks marked, not a traceback
    into_pr_branch = any(s["kind"] == "phase_done" for s in steps)  # only such a plan reports a pr failure instead of stopping
    i = 0
    while i < len(steps):
        step = steps[i]
        if step["kind"] == "refuse":
            print(f"refused: {step['reason']}")
            return 2, reached, pr
        if step["kind"] == "note":
            if step.get("unmerged") or _TICKET_GRANULARITY_NOTE in step["reason"]:
                print(step["reason"])
            i += 1
            continue
        if pr_failed is not None and step["kind"] not in _AFTER_PR_FAILURE:
            # The tasks are marked merged and the phase recorded; the final merge must not run on a PR that is not ready.
            print("stopped; remaining: " + ", ".join(s["kind"] for s in steps[i:]))
            _note_stop(repo, pr_failed[0], pr_failed[1], stops)
            return 1, reached, pr
        carried = _task_already_on_main(repo, step, record) if step["kind"] == "cherry_pick" else None
        walk = None if carried is None else _carried_walk(steps[i:], planned, carried)
        if walk is not None:
            steps, planned, pr = walk
            print(land.landed_via_line(pr))
            i = 0
            continue
        held = guard(step["kind"]) if guard is not None else None
        if held is not None:
            print(held)
            print("stopped; remaining: " + ", ".join(s["kind"] for s in steps[i:]))
            return 2, reached, pr
        reached.append(step["kind"])
        if step["kind"] in ("cherry_pick", "squash_phase", "reuse_branch", "patch_apply", "pr_branch_cut", "merge_phase", "batch_squash"):
            built.append(step.get("onto") or step["branch"])
        if step["kind"] == "mark_merged":
            pr_branch = next((s["pr_branch"] for s in steps if s["kind"] == "phase_done"), "")
            step = {**step, "pr_branch": pr_branch, "by": by, "mode": mode}
        if step["kind"] == "phase_done":
            rec = record or {}
            step = {**step, "phase_record_id": rec.get("run_id") or (f"{rec['run']}:{rec['phase']}" if rec.get("run") and rec.get("phase") else None)}
        if step["kind"] == "merge":
            # gh and git cannot delete a branch a worktree still holds.
            for branch in built:
                _remove_land_worktree(repo, branch)
            merged, merge_detail, halt = _merge_updating(repo, steps, i, pr, forge_module, guard, lock=lock)
            if halt is not None:
                # `_note_stop` prints the reason; printing it here too would say it twice.
                print("stopped; remaining: " + ", ".join(s["kind"] for s in steps[i:]))
                _note_stop(repo, step, halt, stops)
                return 1, reached, pr
        if step["kind"] == "mark_done" and step.get("recorded") is False:
            step = {**step, "pr": pr, "at": datetime.datetime.now(datetime.UTC).isoformat(), "by": by, "mode": mode}
        elif step["kind"] == "mark_done":
            if record is not None and not Path(step["path"]).exists():
                # The store copy is written out as the record file, so the path-derived run, phase and runs_dir below keep working.
                Path(step["path"]).parent.mkdir(parents=True, exist_ok=True)
                Path(step["path"]).write_text(json.dumps(record, indent=2), encoding="utf-8")
            step = {**_mark_done_facts(step, pr, datetime.datetime.now(datetime.UTC).isoformat()), "by": by, "mode": mode}
        raised = False  # the step raised after the merge; a returned failure keeps today's stop
        if step["kind"] == "merge":
            ok, detail = merged, merge_detail
        elif not merge_ran:
            ok, detail = _execute_land_step(repo, step, forge_module)
        else:
            try:
                ok, detail = _execute_land_step(repo, step, forge_module)
            except Exception as exc:
                ok, detail, raised = False, f"{type(exc).__name__}: {exc}", True
        if step.get("before") == "pr_create":
            # The sync may have just written `issue:`; the PR opened next must carry its `Closes`.
            steps = land.with_issue(steps, record, _land_item_facts(item_path)[1])
        pr = _landed_pr(step["kind"], ok, detail, pr, step.get("pr"))
        if not ok and into_pr_branch and step["kind"] in _PR_REPORT_STEPS:
            print(f"{step['kind']}: {detail}")
            print(f"warning: {step['kind']} failed after the push; the phase is merged into the pr branch")
            pr_failed = (step, detail)
            i += 1
            continue
        if step["kind"] == "checks" and not ok and detail.startswith(land.LAUNCH_ERROR):
            # A check whose executable `subprocess` can't find is a refusal,
            # not an ordinary failure, and it fires before `push` so a check
            # that never ran leaves no pushed branch behind.
            print(detail)
            _note_stop(repo, step, detail, stops)
            return 2, reached, pr
        print(f"{step['kind']}: {detail}")
        if ok and step["kind"] == "merge":
            merge_ran = True
            landing = _landing_ref(detail, pr, _pr_number(steps, i, pr), step.get("default_branch", "main"))
        if ok and step["kind"] == "mark_done":
            initiative = _step_initiative(step, record)
            if not landing:
                print(f"review PRs of {step['task']} not closed: no merge ran in this land")
            elif not initiative:
                print(f"warning: review PRs of {step['task']} not closed: no initiative on its record")
            else:
                for line in _close_review_prs(repo, initiative, step["task"], landing, forge_module):
                    print(line)
        if not ok and step["kind"] in ("cherry_pick", "squash_phase") and detail.startswith(_GENERATE_FAILED):
            # `_execute_land_step` detached it and deleted its branch; the next land's same step removes it.
            built.remove(step["onto"])
            print(f"land: worktree left at {_land_worktree(repo, step['onto'])} for inspection")
        if not ok and step["kind"] == "merge" and not looped and land.merge_expects_check(detail):
            # A required check had not reported when the merge ran; wait once more, then retry the merge.
            waits = [j for j in range(i) if steps[j]["kind"] == "wait_checks"]
            if waits:
                looped = True
                print("land: merge refused for an expected required check; waiting for checks once more")
                i = waits[-1]
                continue
        if raised or (not ok and step["kind"] == "mark_done" and pr):
            # The PR is merged: record it and mark every task whose mark_done has not run, so none keeps holding its files.
            number = _pr_number(steps, i, pr)
            pr = pr or (f"#{number}" if number is not None else "")
            at = datetime.datetime.now(datetime.UTC).isoformat()
            for task, why in _mark_remaining_done(steps[i:], pr, at, lambda s: _execute_land_step(repo, {**s, "by": by, "mode": mode}, forge_module)):
                print(f"{task} not marked done: {why}")
        if not ok:
            remaining = [s["kind"] for s in steps[i + 1:]]
            print("stopped; remaining: " + ", ".join(remaining))
            _note_stop(repo, step, detail, stops)
            return 1, reached, pr
        if step["kind"] == "cherry_pick" and "task" not in step and land.is_skipped_all(detail):
            # Every commit is on the base already: close through the PR that carried them, with no push, checks or merge.
            number = _task_already_on_main(repo, step, record)
            walk = None if number is None else _carried_walk(steps[i + 1:], planned, number)
            if walk is None:
                why = f"{detail}, but no pull request number was found on {step['from']} for {step['branch']}"
                print("stopped; remaining: " + ", ".join(s["kind"] for s in steps[i + 1:]))
                _note_stop(repo, step, why, stops)
                return 1, reached, pr
            steps, planned, pr = walk
            print(land.landed_via_line(pr))
            i = 0
            continue
        if step["kind"] == "wait_checks" and no_merge:
            print("stopping after wait_checks (--no-merge)")
            return 0, reached, pr
        i += 1
    if pr_failed is not None:
        _note_stop(repo, pr_failed[0], pr_failed[1], stops)
        return 1, reached, pr
    stop = land.gate_stop(planned, steps, level, pr)
    if stop:
        print(stop)
    return (3 if stop else 0), reached, pr


def _leader_read_or_refuse(runs_dir: Path):
    """A file present but unreadable or not valid JSON is a refusal, not a free lock
    (charter A6: `chair.read` raises at the edge; here is where that becomes a value).
    Returns `(record_or_none, None)` or `(None, 2)` after printing the reason."""
    try:
        return chair.read(runs_dir), None
    except (OSError, json.JSONDecodeError) as exc:
        print(f"chair: lock file unreadable ({type(exc).__name__}: {exc})")
        return None, 2


def _leader_guard_or_refuse(runs_dir: Path, holder: str, force: bool, claim: bool = False, explicit_label: str | None = None) -> int | None:
    """Exit code 2 with the refusal printed when another live session holds the loop;
    None to proceed. `claim` takes an unheld, stale or crashed lock for `holder` before
    proceeding, so launching or landing is what makes a session the leader."""
    record, rc = _leader_read_or_refuse(runs_dir)
    if rc is not None:
        return None
    state = chair.liveness(record, _leader_pid_alive(record), datetime.datetime.now(datetime.UTC), socket.gethostname(), _leader_heartbeat_minutes())
    holder = chair.resolve_holder_label(record, state, os.environ, socket.gethostname(), explicit_label) or holder
    line = chair.guard(record, holder, state)
    if line is not None:
        print(f"override: {line}" if force else line)
        return None if force else 2
    if claim and state != "live":
        pid, host = _leader_identity(None, os.environ)
        new_record, _ = chair.take(record, holder, pid, host, datetime.datetime.now(datetime.UTC), _leader_heartbeat_minutes(), _leader_pid_alive(record), steal=True, claude_session=chair.claude_session_from_env(os.environ))
        chair.write(runs_dir, new_record)
        print(f"taking the loop: {holder}" if record is None else f"taking the loop from {record.get('session')} ({state})")
    return None


def _leader_pid_choice(explicit_pid: int | None, env: Mapping[str, str], ppid: int, alive: Callable[[int], bool]) -> tuple[int, bool]:
    """Pure. The pid to record and whether it is the parent-pid fallback.
    An explicit pid wins, then an all-digit, non-zero, live `CLAUDE_PID`, then `ppid`."""
    if explicit_pid:
        return explicit_pid, False
    raw = env.get("CLAUDE_PID", "")
    if raw.isascii() and raw.isdigit() and int(raw) > 0 and alive(int(raw)):
        return int(raw), False
    return ppid, True


def _leader_identity(explicit_pid: int | None = None, env: Mapping[str, str] | None = None) -> tuple[int, str]:
    """The pid and host that name *this session* to the lock, for `take`/`beat`/`release`.
    `os.getpid()` names this one `cox` invocation, which exits the moment the command
    returns. `os.getppid()` names the process that invoked `cox`, which is the invoking
    shell and not the loop: a caller that runs each command in a fresh shell records a
    pid that is dead before the next command, so its lock reads as stale at once.
    `--pid`, or a live `CLAUDE_PID` in the passed `env`, names the durable process instead.
    Every verb that writes or matches a lease passes `os.environ`, so they all name the same pid;
    no `env` means no `CLAUDE_PID` lookup."""
    return _leader_pid_choice(explicit_pid, env or {}, os.getppid(), chair.pid_alive)[0], socket.gethostname()


def _leader_pid_alive(record: dict | None) -> bool:
    """A pid is judged dead only on the host that recorded it: this process has no way
    to check a pid number on another machine, so a foreign host's lock is never read as
    dead here — only its own host, or its heartbeat aging out, can make it stale."""
    if record is None:
        return False
    pid = record.get("pid")
    if not isinstance(pid, int):
        return False
    if record.get("host") != socket.gethostname():
        return True
    return chair.pid_alive(pid)


def _check_capacity(runs_dir: Path, profile: dict, hostname: str) -> int | None:
    """Edge. The capacity a check's worker count divides cores by: `COX_LANE_CAPACITY` when set, else `_local_max_in_flight`.
    A set value that is not a whole number of 1 or more is None, so the checks run uncapped, as `lane_workers_from` reads it."""
    raw = os.environ.get("COX_LANE_CAPACITY")
    if raw is None:
        return _local_max_in_flight(runs_dir, profile, hostname)
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if value >= 1 else None


def _unusable_branch(text: str, path: Path) -> str | None:
    """Pure. Why the `target_branch:` in one epic file's text is set and not a valid branch name, else None."""
    from core.landing import valid_branch  # lazy: core sits beside agent_tools in one venv
    from core.workstore import WorkStoreError, split_frontmatter

    try:
        branch = split_frontmatter(text, path)[0].get("target_branch")
    except WorkStoreError:
        return None
    if branch is None:
        return None
    try:
        valid_branch(branch, "epic")
    except ValueError as exc:
        return str(exc)
    return None


def _read_epics(ws: Path) -> tuple[dict[str, dict], dict[str, str], set[str]]:
    """Edge. Each `<ws>/epics/*.md` read on its own, so one broken file never changes another epic:
    `(definitions, refusals, unread)`. A file that cannot be read or parsed is a `note:` on stderr and its stem is in
    `unread`. A file whose `target_branch` is set and not a valid branch name is in `refusals`, keyed by the epic id
    (the file stem), with the file and the value in the message."""
    from core.epic_files import parse_epic  # lazy: core sits beside agent_tools in one venv

    definitions: dict[str, dict] = {}
    refusals: dict[str, str] = {}
    unread: set[str] = set()
    directory = ws / "epics"
    for path in sorted(directory.glob("*.md")) if directory.is_dir() else []:
        text = ""
        try:
            text = path.read_text(encoding="utf-8")
            definition = parse_epic(text, path.name)
        except (OSError, ValueError) as exc:
            reason = None if isinstance(exc, OSError) else _unusable_branch(text, path)
            if reason is not None:
                refusals[path.stem] = f"{path}: {reason}"
            else:
                first_line = (str(exc).splitlines() or [type(exc).__name__])[0]
                print(f"note: epic file {path} not read ({first_line}); using the repo default for it", file=sys.stderr)
                unread.add(path.stem)
            continue
        definitions[definition["id"]] = definition
    return definitions, refusals, unread


def _initiative_target_branch(profile: dict, repo: Path | None, ws: Path, initiative: str | None) -> tuple[str | None, str | None]:
    """Edge. `(branch, refusal)`: the initiative's resolved target branch (its own `target_branch:`, else its epic's,
    else the cartridge's `landing_areas.target_branch`), None when none is set. `refusal` is the message when the
    cartridge does not resolve or a set target branch is invalid; every other gap falls back to None with a `note:`."""
    from core.cartridge import OVERLAY_ALLOWED_KEYS  # lazy: core sits beside agent_tools in one venv
    from core.landing import resolve_epic_stack, resolve_target_branch

    cartridge, error = _team_cartridge(profile, repo, OVERLAY_ALLOWED_KEYS)
    if error:
        return None, error
    definitions, refusals, unread = _read_epics(ws)
    try:
        text = _read_text_or_none(ws / "work" / initiative / "initiative.md") if initiative else None
        fields = route.parse_frontmatter(text)[0] if text is not None else {}
        if text is not None and not fields:
            print(f"note: work/{initiative}/initiative.md has no front matter; using the repo default", file=sys.stderr)
        epic_id = fields.get("epic")
        if "epic" in fields and not isinstance(epic_id, str):
            print(f"note: work/{initiative}/initiative.md epic: is not a string; ignoring it", file=sys.stderr)
            epic_id = None
        if epic_id in refusals:
            return None, refusals[epic_id]
        epic = definitions.get(epic_id) if epic_id else None
        if epic_id and epic is None and epic_id not in unread:
            print(f"note: epic {epic_id} has no file under epics/; using the repo default", file=sys.stderr)
        resolved = resolve_target_branch(cartridge, epic["target_branch"] if epic else None, fields.get("target_branch"))
        if epic and initiative and not fields.get("target_branch") and resolve_epic_stack(cartridge, epic.get("epic_stack")):
            # A stacked epic: an initiative that needs an unlanded initiative of the same epic forks from its branch.
            from agent_tools import lander_ports
            resolved = lander_ports.stack_base(
                ws, initiative, str(epic_id), epic["target_branch"], lander_ports.read_records(ws / "runs")
            ) or resolved
        return resolved, None
    except ValueError as exc:
        return None, str(exc)


def _land_initiative(runs_dir: Path, run_id: str, phase: str | None, task: str | None) -> str | None:
    """Edge. The initiative a land is for: the phase's, else the task record's, else the run id's own."""
    if phase:
        return _initiative_of_phase(runs_dir.parent / "work", phase, run_id)
    record = _land_load(runs_dir, run_id, task)[0]
    return (record or {}).get("initiative") or chair_facts.run_initiative(run_id)


def _phase_land_at(phase_record: Mapping, profile: dict, work_dir: Path, initiative: str) -> str:
    """Edge. The phase record's `land_at` when its run wrote one, else what the chair plans the land with: the
    initiative's `land_at:`, else the team cartridge's, else ticket. The run store's phase manifests carry no
    `land_at`, so reading the record alone gave ticket while the chair planned at level: the chair asked for a
    phase land, the land deferred it as ticket and exited 0 with nothing done, and the chair backed off."""
    if phase_record.get("land_at") in land.LAND_AT:
        return land.granularity(phase_record)
    from core.landing import resolve_land_at  # lazy: core sits beside agent_tools in one venv

    path = work_dir / initiative / "initiative.md"
    text = _read_text_or_none(path)
    values = _initiative_land_at({path: text}) if text is not None else {}
    try:
        return resolve_land_at(_team_cartridge(profile)[0], values.get(initiative))
    except ValueError:
        return land.granularity(phase_record)


def _profile_checkout(profile: Mapping) -> str | None:
    """Pure. The one checkout of a monorepo profile: `umbrella_dir` when `harness_dir` lies inside it; None for a
    profile of the component layout, whose umbrella held no code."""
    umbrella, harness = profile.get("umbrella_dir"), profile.get("harness_dir")
    if not umbrella or not harness:
        return None
    root, inner = Path(umbrella).expanduser(), Path(harness).expanduser()
    return str(root) if inner.is_relative_to(root) and inner != root else None


def _cartridges_checkout(profile: Mapping) -> str:
    """The checkout that holds `provider_profile`; `cartridges_dir` when the profile names no provider profile."""
    provider, cartridges = profile.get("provider_profile"), profile.get("cartridges_dir")
    if provider:
        return str(Path(provider).expanduser().parent)
    return str(Path(cartridges).expanduser()) if cartridges else ""


def _host_repos(profile: Mapping) -> list[str]:
    """Edge: the harness, cartridges and tools checkouts, the three paths the chair itself runs from; on a migrated
    host they are folders of one coxswain checkout, named once, at its root."""
    harness = str(Path(profile["harness_dir"]).expanduser()) if profile.get("harness_dir") else ""
    paths = [p for p in (harness, _cartridges_checkout(profile), str(Path(__file__).resolve().parents[1])) if p]
    return install_root.collapse_to_checkouts(paths, {p: install_root.checkout_root(p) for p in paths})


def _repo_is_detached(repo: Path) -> bool:
    """Edge: True when `git symbolic-ref -q HEAD` fails; a path that is no checkout reads as detached too."""
    return bounded_run.completed(["git", "-C", str(repo), "symbolic-ref", "-q", "HEAD"], capture_output=True).returncode != 0


def _install_roots(profile: Mapping) -> list[str]:
    """Edge: the checkouts the chair runs from: `umbrella_dir`, the monorepo checkout, and the host repos."""
    roots = [profile.get("umbrella_dir"), _profile_checkout(profile), *_host_repos(profile)]
    return [str(r) for r in roots if r]
