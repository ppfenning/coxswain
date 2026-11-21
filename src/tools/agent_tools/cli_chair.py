"""`cox chair`: the chair's facts, its gathering and the dependencies `chair run` acts through."""

from __future__ import annotations

import collections
import contextlib
import dataclasses
import datetime
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Collection, Hashable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_tools.cli_board import _chair_export_hook
from agent_tools.cli_common import _initiative_texts, _read_text_or_none, _stored_work_items, workspace_git_on
from agent_tools.cli_land import _check_capacity, _read_epics
from agent_tools.cli_lazy import (
    chair,
    chair_actions_cache,
    chair_capacity,
    chair_dash_sync,
    chair_decompose_runs,
    chair_exec,
    chair_facts,
    chair_load_warn,
    chair_orchestrate,
    chair_phase_quarantine,
    chair_pid_probe,
    chair_read_approved,
    chair_read_artifacts,
    chair_read_attempts,
    chair_read_cache,
    chair_read_decompose_miss,
    chair_read_docket,
    chair_read_exits,
    chair_read_housekeeping,
    chair_read_idle_backlog,
    chair_read_idle_hosts,
    chair_read_idle_lands,
    chair_read_idle_open,
    chair_read_intake,
    chair_read_lease,
    chair_read_live,
    chair_read_lost,
    chair_read_patch,
    chair_read_phase_queue,
    chair_read_phase_records,
    chair_read_quarantined,
    chair_read_queues,
    chair_read_record,
    chair_read_remote_unfetched,
    chair_read_run_id,
    chair_read_run_outcomes,
    chair_read_run_records,
    chair_read_stale,
    chair_read_stall,
    chair_read_stranded,
    chair_report,
    chair_restart,
    chair_restart_log,
    chair_revert_green,
    chair_revert_ports,
    chair_revert_watch,
    chair_run,
    chair_smoke,
    chair_stall_alarm,
    chair_tick_cache,
    chair_wake,
    checkout_sync,
    courier,
    draft_list,
    host_cmd,
    install_root,
    intake_state,
    land_limit,
    lane_checkout_sync,
    lane_hosts,
    notify_core,
    pacing,
    queue_export,
    route,
    route_guard,
    route_sync_gh,
    run_logs,
    run_store,
    store_cli,
    usage_meter,
    usage_window,
    workspace_sync,
)
from agent_tools.cli_leader import _chat_deps
from agent_tools.cli_route import _live_runs
from agent_tools.cli_route_launch import _lane_sync_ssh, _live_by_host, _pid_probe_ssh, _shadowed_note
from agent_tools.cli_route_sync import (
    _chair_max_in_flight,
    _initiative_land_at,
    _initiative_repos,
    _local_max_in_flight,
    _run_argv,
    _team_cartridge,
)
from agent_tools.cli_runs import _approved_branch_merged, _repo_for, _wrongly_released_merged
from agent_tools.cli_runs_fetch import _lane_host_candidates, _profile_lane_hosts, _store_held_records
from agent_tools.cli_stats import _json_or
from agent_tools.cli_usage import _chair_meter, _profile_month, _reconcile_ceilings, _resolved_pacing_policy
from agent_tools.store_dialect import is_postgres


@dataclasses.dataclass(frozen=True)
class _ChairUnwired:
    """A reader or recorder with no edge yet; `_chair_run` refuses to start while any field of either bundle holds one."""

    source: str

    def __call__(self, *_args: object) -> object:
        raise RuntimeError(f"chair run: the {self.source} source is not wired to a reader yet")


def _chair_unwired_sources(deps: chair_run.RunDeps) -> list[str]:
    """Named unwired fields of the facts and exec bundles, in field order."""
    return [
        v.source
        for bundle in (deps.facts_deps, deps.exec_deps)
        for f in dataclasses.fields(bundle)
        if isinstance(v := getattr(bundle, f.name), _ChairUnwired)
    ]


def _chair_once_exit(last_line: str) -> int:
    """1 when the status line has the shape `chair_run.error_line` prints; a test builds one with it, so a format change fails there."""
    return 1 if "| tick error: " in last_line else 0


def _chair_stranded_inputs(
    ws: Path, mode: str, files: Mapping[Path, str] | None = None, store_rows: list | None = None,
) -> tuple[list[dict], list[dict]]:
    """Edge. The inputs of `runs_stranded.stranded`. Item states come from the store under mode "store", as the docket's do.

    Under mode "store" a fetched run whose records stayed in the store adds its store rows too: without them an
    approved task of that run gets no run, and `plan_lands` can only ask the chair for one.

    `files` (`chair_read_quarantined.read_work_files`) and `store_rows` (`run_store.work_items`) stand in for the
    work-file and store reads when a tick already holds them; None reads them here."""
    task_records = []
    for path in sorted((ws / "runs").glob("*/tasks/*/*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        record.setdefault("run", path.parent.parent.parent.name)
        record.setdefault("task", path.stem)
        record.setdefault("phase", path.parent.name)
        task_records.append(record)
    if files is None:
        initiative_texts, work_items = _initiative_texts(ws), _stored_work_items(ws, mode)
    else:
        initiative_texts = {p.parent.name: t for p, t in chair_read_quarantined.initiative_files(ws, files).items()}
        rows = store_rows if store_rows is not None else (run_store.work_items(ws / "runs") if mode == "store" else [])
        work_items = route.with_store_states(
            [
                route.work_item(route.parse_frontmatter(text)[0], initiative=p.parent.parent.name, phase_dir=p.parent.name, stem=p.stem)
                for p, text in chair_read_quarantined.task_files(ws, files).items()
                if p.name != "initiative.md"
            ],
            rows, mode,
        )
    items = [
        {**item, "repo": route.parse_frontmatter(initiative_texts.get(item["initiative"], ""))[0].get("repo")}
        for item in work_items
    ]
    stored = _store_held_records(ws / "runs", items) if mode == "store" else []
    return task_records + stored, items


class _TargetBranchRefusal(Exception):
    """A target branch is set and cannot be used; the message names the file, the value and the initiatives affected."""


def _epic_users(ws: Path) -> dict[str, list[str]]:
    """Edge. Epic id to the initiatives whose `initiative.md` names it in a string `epic:`."""
    users: dict[str, list[str]] = {}
    for path in sorted((ws / "work").glob("*/initiative.md")):
        text = _read_text_or_none(path)
        epic = route.parse_frontmatter(text)[0].get("epic") if text is not None else None
        if isinstance(epic, str):
            users.setdefault(epic, []).append(path.parent.name)
    return users


def _epic_definitions(ws: Path) -> dict[str, dict]:
    """Edge. The valid epic definitions of `ws`, for the chair. A bad `target_branch` in the file of an epic that an
    initiative names raises `_TargetBranchRefusal`, as `_initiative_target_branch` refuses that initiative. A bad file
    no initiative names is a `note:`, so it cannot stop the chair for unrelated epics."""
    definitions, refusals, _ = _read_epics(ws)
    users = _epic_users(ws) if refusals else {}
    for epic_id, message in refusals.items():
        if epic_id in users:
            raise _TargetBranchRefusal(f"{message} (epic of initiative {', '.join(users[epic_id])})")
        print(f"note: epic target branch unusable ({message}); no initiative names that epic", file=sys.stderr)
    return definitions


def _store_nap(runs_dir: Path) -> Callable[[float], bool] | None:
    """Edge. The chair's wait step on the store's notifications (`harness.store_notify`, migration 0021): blocks up to
    the step, True once a table in `chair_wake.STORE_WAKE_TABLES` was written. None where the store cannot notify
    (sqlite) or cannot be reached now, and a listener that fails later sleeps the step out: polling stays the fallback."""
    from harness import store_notify

    try:
        listener = store_notify.listen(run_store.store_url_for(runs_dir))
    except Exception:
        return None
    if listener is None:
        return None

    def nap(seconds: float) -> bool:
        try:
            return bool(listener.wait(seconds) & chair_wake.STORE_WAKE_TABLES)
        except Exception:
            time.sleep(seconds)
            return False

    return nap


def _chair_gather(ws: Path) -> chair_run.Gather:
    """Pure. The chair's gatherer with the epic definitions of `ws` read on every tick through `_epic_definitions`."""
    return lambda deps, now: chair_facts.gather_facts(deps, now, epic_definitions=_epic_definitions(ws))


def _chair_reported_repos_path(runs_dir: Path) -> Path:
    """Beside `chair.lease.json`: the persisted set of repo paths already reported missing."""
    return runs_dir / "chair.reported_repos.json"


def _read_reported_repos(runs_dir: Path) -> set[str]:
    raw = _json_or(_read_text_or_none(_chair_reported_repos_path(runs_dir)), [])
    return set(raw) if isinstance(raw, list) else set()


def _write_reported_repos(runs_dir: Path, repos: list[str]) -> None:
    _chair_reported_repos_path(runs_dir).write_text(json.dumps(sorted(set(repos))), encoding="utf-8")


def _handled_keys_path(runs_dir: Path) -> Path:
    """Beside `chair.lease.json`: the sort keys the service already acted on, so a failure is rebuilt or annotated once."""
    return runs_dir / "chair.handled_keys.json"


def _read_handled_keys(runs_dir: Path) -> list[str]:
    raw = _json_or(_read_text_or_none(_handled_keys_path(runs_dir)), [])
    return [k for k in raw if isinstance(k, str)] if isinstance(raw, list) else []


def _mark_handled_key(runs_dir: Path, key: str) -> None:
    """Add `key`, through a temp file renamed over the old one: a damaged file reads as no keys, which would announce
    every hold and ask every triage again. A write cut short leaves the old file and no temp file, and raises."""
    keys = _read_handled_keys(runs_dir)
    if key in keys:
        return
    path = _handled_keys_path(runs_dir)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps([*keys, key]))
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _dispatch_counts(lanes: Sequence[run_store.Lane], local: str, listed: Sequence[str], pidfile_live: int) -> dict[str, int]:
    """`lanes` are the store lanes no local pidfile names. Live pidfile runs and lanes on a host the profile does not list count as local, so the total is what `busy_lanes` counted."""
    by_host = _live_by_host(lanes, local)
    return {"": pidfile_live + sum(n for name, n in by_host.items() if name not in listed), **{name: by_host.get(name, 0) for name in listed}}


def _record_beat_wanted(dry_run: bool, lost: str) -> bool:
    """The record file is beaten only by a live run whose lease renewal came back without a refusal."""
    if dry_run:
        return False
    return lost == ""


def _beat_record(runs_dir: Path, session: str, pid: int, host: str, now: datetime.datetime) -> None:
    """Edge. One loop beat of chair.json under the lock: moves `heartbeat_at` and, through the record's window, `until`."""
    with chair.locked(runs_dir):
        record = chair.read(runs_dir)
        moved, _ = chair.beat(record, session, pid, host, now, extend_window=True)
        # `renew_lease` returning no refusal already proves this session, pid and host
        # hold the store lease -- including right after a take_lease reclaim, which
        # rewrites only the lease sidecar (agent_tools/chair_exec.py `_lease`) and never
        # chair.json. `chair.beat` then refuses on a record that still names the prior
        # holder, so `moved` reads None though the lease is genuinely ours; adopt the
        # record here rather than leave heartbeat_at frozen behind a lease we hold.
        if moved is None:
            stamp = now.isoformat()
            moved = {
                "session": session, "pid": pid, "host": host,
                "taken_at": stamp, "heartbeat_at": stamp,
                "runs": [], "claude_session": chair.claude_session_from_env(os.environ),
            }
        chair.write(runs_dir, moved)


def _landed_commits(result: chair_exec.Result) -> list[tuple[str, str, str]]:
    """(task id, commit, land output) for every commit a result put on main. A partial_land lists one per task land in
    `landed_commits`, whatever its own status (an earlier task can land before a later one fails); any other land is
    one landed result with a commit."""
    if "landed_commits" in result:
        return [(c["task_id"], c["commit"], c["reason"]) for c in result["landed_commits"] if c.get("commit")]
    if result["status"] == "landed" and result.get("commit"):
        return [(result["action"].get("task_id", ""), result["commit"], result["reason"])]
    return []


def _land_watches(results: list[chair_exec.Result], tickets: Callable[[], Sequence[Mapping]]) -> list[dict]:
    """One watch per landed commit: one per landed result, or one per task land of a partial_land. A land_phase or
    partial_land names its phase; a plain land takes it from its ticket. The tickets are read only when a land needs them."""
    landed = [(r["action"], *c) for r in results for c in _landed_commits(r)]
    if not landed:
        return []
    phase_of = {(t.get("initiative", ""), t.get("id", "")): t.get("phase", "") for t in tickets()}
    return [
        {
            "initiative": action.get("initiative", ""),
            "phase": action.get("phase") or phase_of.get((action.get("initiative", ""), task_id), ""),
            "repo": action.get("repo", ""),
            "pr": chair_exec._pr_from_reason(reason),
            "commit": commit,
        }
        for action, task_id, commit, reason in landed
    ]


def _run_argv_output(argv: list[str]) -> tuple[int, str]:
    code, out, err = _run_argv(argv)
    return code, out or err


def _rebuild_dash_after_land(
    watches: Sequence[Mapping], dash_dir: str | None, bin_dir: str, toolchain_found: bool,
    run: Callable[[list[str]], tuple[int, str]], echo: Callable[[str], None],
) -> None:
    """Edge. Pull, build and link towpath once per distinct landed repo that is the dash checkout. A failure is one
    echoed line: it never raises, holds no land and does not touch the smoke."""
    for repo in dict.fromkeys(w.get("repo", "") for w in watches):
        try:
            argvs = chair_dash_sync.rebuild_argvs(repo, dash_dir, bin_dir, toolchain_found)
            code = chair_dash_sync.run_argvs(argvs, run) if argvs else 0
            if code != 0:
                echo(f"chair run: towpath rebuild failed (exit {code}) for {repo}")
        except Exception as exc:
            with contextlib.suppress(Exception):
                echo(f"chair run: towpath rebuild failed for {repo}: {exc}")


def _chair_perform_with_smoke(
    runs_dir: Path, perform: chair_run.Perform,
    watch: Callable[[Mapping], None] = lambda _w: None, held: Callable[[], None] = lambda: None,
    tickets: Callable[[], Sequence[Mapping]] = lambda: [],
    *, dash_dir: str | None = None, bin_dir: str = "", toolchain_found: bool = False,
    run: Callable[[list[str]], tuple[int, str]] = _run_argv_output, echo: Callable[[str], None] = lambda _l: None,
) -> chair_run.Perform:
    """Wraps `perform` with the post-land watch and smoke. A live run records a watch for every landed commit, then a
    landed coxswain (or coxswain-tools/-graphs) PR runs the fixed smoke commands once against the first triggering land's repo.
    A failure holds a `HoldRecord` and opens no revert PR: the hold makes `landed_main` report smoke failed for that
    commit, so the next tick's plan emits `revert_land`. A pass clears an existing hold. `held` runs last, to resolve
    the watches whose main went green. A dry run calls neither `watch` nor `held`. With a `dash_dir`, a live run also
    rebuilds towpath after a land in that checkout; the default `dash_dir` of None plans nothing.
    """

    def wrapped(
        actions: list[chair_exec.Action], deps: chair_exec.Deps, current_epoch: Callable[[], int], dry_run_: bool,
    ) -> list[chair_exec.Result]:
        results = perform(actions, deps, current_epoch, dry_run_)
        if not dry_run_:
            watches = _land_watches(results, tickets)
            for w in watches:
                watch(w)
            _rebuild_dash_after_land(watches, dash_dir, bin_dir, toolchain_found, run, echo)
        triggers = chair_exec.smoke_targets(results)
        if triggers:
            land = triggers[0]
            ok, failing = chair_smoke.smoke_verdict(chair_smoke.run_smoke_commands(land["repo"]))
            if not ok:
                chair_smoke.write_hold(str(runs_dir), chair_smoke.hold_record(land, failing))
            elif chair_smoke.read_hold(str(runs_dir)) is not None:
                chair_smoke.clear_hold(str(runs_dir))
        if not dry_run_:
            held()
        return results

    return wrapped


def _gh_repo(repo: str) -> str:
    """`gh --repo` needs owner/name; a land watch carries a checkout path. An unresolvable value passes through."""
    try:
        return route_sync_gh._repo_of(repo)
    except route_sync_gh.RepoError:
        return repo


def _main_ci_reader(runner: Callable[[list[str]], tuple[int, str]]) -> Callable[[str, str], tuple[str, str]]:
    """`read(repo, commit)` is `main_ci` that never raises: a gh failure reads as pending with the error text.
    Results cache per (repo, commit) in `read.cache`, which the caller clears each tick."""
    cache: dict[tuple[str, str], tuple[str, str]] = {}

    def read(repo: str, commit: str) -> tuple[str, str]:
        key = (repo, commit)
        if key not in cache:
            try:
                cache[key] = chair_revert_ports.main_ci(runner, _gh_repo(repo), commit)
            except Exception as exc:  # any gh or parse failure must not escape gather_facts
                cache[key] = ("pending", f"{type(exc).__name__}: {exc}")
        return cache[key]

    read.cache = cache  # type: ignore[attr-defined]
    return read


def _green_since_reader(runner: Callable[[list[str]], tuple[int, str]]) -> Callable[[str, str], bool]:
    """`read(repo, commit)` is `main_green_since` that never raises. Results cache per (repo, commit) in `read.cache`,
    which the caller clears each tick; the git ancestry answers behind them are kept for the process."""
    cache: dict[tuple[str, str], bool] = {}
    ancestry: dict[tuple[str, str, str], bool] = {}  # process-lifetime, unlike `cache`: an ancestry answer never changes

    def read(repo: str, commit: str) -> bool:
        key = (repo, commit)
        if key not in cache:
            try:
                cache[key] = chair_revert_green.main_green_since(
                    runner, repo, _gh_repo(repo), commit, ancestry, CHAIR_ANCESTRY_CACHE_MAX
                )
            except Exception:  # any gh or git failure must not escape gather_facts
                cache[key] = False
        return cache[key]

    read.cache = cache  # type: ignore[attr-defined]
    return read


class _SlugForge:
    """A `GhForgePort` whose `open_pr` takes the watch's checkout path and hands gh its owner/name."""

    def __init__(self, inner: chair_revert_ports.GhForgePort) -> None:
        self._inner = inner

    def open_pr(self, repo: str, branch: str, title: str, body: str) -> str:
        return self._inner.open_pr(_gh_repo(repo), branch, title, body)

    def wait_checks(self, pr: str) -> list[str]:
        return self._inner.wait_checks(pr)

    def merge(self, pr: str) -> None:
        self._inner.merge(pr)


def _revert_ports(
    runner: Callable[[list[str]], tuple[int, str]],
) -> Callable[[chair_exec.Action], tuple[chair_revert_ports.GitRevertPort, _SlugForge]]:
    return lambda action: (
        chair_revert_ports.GitRevertPort(runner), _SlugForge(chair_revert_ports.GhForgePort(runner)),
    )


def _quarantine_phase_with(
    rows: Callable[[str], Sequence[Mapping]],
    run_ids: Callable[[], Iterable[str]],
    attempt_runs: Callable[[str, str, str], Iterable[str]],
    move: Callable[[str, str, str], object],
    mark: Callable[[str, str, Mapping], str],
) -> Callable[[str, str, str, str], chair_phase_quarantine.Quarantined]:
    """Quarantines the tasks of a reverted phase that its land carried (`chair_phase_quarantine.phase_targets`), each by a
    compare-and-set on the state read (`move`), then marks each moved row with the cause (`mark`), so the next tick reads
    the quarantine as that cause. Answers the tasks it moved and one line per write the store did not take, so
    `_revert_land` names both.

    `rows` is an initiative's task rows; `attempt_runs(initiative, phase, task)` the runs of the task's own recorded
    attempts, which with `run_ids` set the mark's run (`chair_phase_quarantine.mark_run`); `move(initiative, task,
    expected)` answers a `store_cli.SetStateResult`; `mark(initiative, task, extra)` answers "" when the store took the
    row, else why not."""

    def quarantine(initiative: str, phase: str, cause: str, reason: str) -> chair_phase_quarantine.Quarantined:
        targets = chair_phase_quarantine.phase_targets(rows(initiative), initiative, phase)
        recorded = list(run_ids()) if targets else []
        moved: list[str] = []
        failures: list[str] = []
        for row in targets:
            task = str(row["task_id"])
            written = move(initiative, task, str(row["state"]))
            line = store_cli.state_write_failure(initiative, task, "quarantined", written)  # type: ignore[arg-type]
            if line is not None or not isinstance(written, store_cli.StateSet):
                failures.extend([line] if line is not None else [])
                continue
            moved.append(task)
            run = chair_phase_quarantine.mark_run(recorded, initiative, attempt_runs(initiative, phase, task))
            refused = mark(initiative, task, chair_phase_quarantine.marked_extra(row.get("extra"), cause, reason, run))
            if refused:
                failures.append(f"warning: store did not record the {cause} cause on {initiative}/{task}: {refused}")
        return chair_phase_quarantine.Quarantined(tuple(moved), tuple(failures))

    return quarantine


def _store_quarantine(runs_dir: Path, by: str) -> Callable[[str, str, str, str], chair_phase_quarantine.Quarantined]:
    """Edge. The chair's quarantine of a reverted phase, read from and written to the store. A task's attempt runs are
    the run task records the chair's own attempts reader opens (`chair_read_run_records.read_run_records`)."""

    def attempt_runs(initiative: str, phase: str, task: str) -> list[str]:
        records = chair_read_run_records.read_run_records(runs_dir, {(phase, task)})
        return [run for run, *_ in records]

    return _quarantine_phase_with(
        lambda initiative: run_store.read_queue(runs_dir, initiative, kind="task"),
        lambda: run_store.run_ids(runs_dir),
        attempt_runs,
        lambda initiative, task, expected: store_cli.set_state(runs_dir, initiative, task, "quarantined", by, expected),
        lambda initiative, task, extra: run_store.upsert_row_detail(runs_dir, {"initiative": initiative, "task_id": task, "extra": extra}),
    )


def _revert_reuse(
    runner: Callable[[list[str]], tuple[int, str]],
) -> Callable[[chair_exec.Action], tuple[Callable[[str, int], str | None], Callable[[str, str, bool], list[str]]]]:
    """Edge. A revert_land's lookup of its open `revert/<pr>` PR (by the repo's owner/name) and its stale-branch clear."""
    return lambda action: (
        lambda repo, pr: chair_revert_ports.find_open_revert_pr(runner, _gh_repo(repo), pr),
        chair_revert_ports.GitRevertPort(runner).clear_stale_branch,
    )


def _revert_land_deps(
    runs_dir: Path, holder: str, runner: Callable[[list[str]], tuple[int, str]],
    actions: Callable[[], Sequence[Mapping]], record: Callable[[Mapping], None],
) -> dict[str, object]:
    """Edge. The `chair_exec.Deps` fields a revert_land runs on: its git and forge ports, the store quarantine of the
    reverted phase, and the resolve of its land watch. `cox chair run` wires exactly these."""
    return {
        "revert_ports": _revert_ports(runner),
        "revert_reuse": _revert_reuse(runner),
        "quarantine_phase": _store_quarantine(runs_dir, holder),
        "resolve_land": _resolve_land_with(actions, record),
    }


def _resolve_land_with(
    read: Callable[[], Sequence[Mapping]], write: Callable[[Mapping], None],
) -> Callable[[str, str, str], None]:
    """`resolve_watch` is keyed by commit; a phase's land is found through its pending watches."""

    def resolve(initiative: str, phase: str, outcome: str) -> None:
        for w in chair_revert_watch.pending_watches(read):
            if w["initiative"] == initiative and w["phase"] == phase:
                chair_revert_watch.resolve_watch(write, w["commit"], outcome)

    return resolve


def _initiative_intakes(initiative_texts: Mapping[Path, str]) -> dict[str, str]:
    """Initiative id (its directory name) to the intake path its `initiative.md` names; one naming none is left out."""
    named = {p.parent.name: intake_state.intake_field(text) for p, text in initiative_texts.items()}
    return {initiative: intake for initiative, intake in named.items() if intake}


def _newest_run_ids(run_ids_by_initiative: Mapping[str, Sequence[str]]) -> dict[str, str]:
    """Initiative id to its newest decompose run id. Each list is oldest first, as `read_decompose_runs` returns it."""
    return {initiative: run_ids[-1] for initiative, run_ids in run_ids_by_initiative.items() if run_ids}


def _decompose_initiative_id(intake_id: str, rows: Sequence[Mapping]) -> str:
    """The initiative a decompose of this intake runs under. Once the fresh `id-N` initiative exists the newest live one wins, since `fresh_initiative_id` alone would step past it to `id-N+1`."""
    fresh = route_guard.fresh_initiative_id(intake_id, rows)
    if fresh == intake_id:
        return intake_id
    pattern = re.compile(re.escape(intake_id) + r"-(\d+)")
    live = [
        (int(m.group(1)), row["task_id"])
        for row in rows
        if row.get("kind") == "initiative" and row.get("state") != "dropped"
        for m in [pattern.fullmatch(str(row.get("task_id")))]
        if m
    ]
    return max(live)[1] if live else fresh


def _task_item_counts(tickets: Sequence[Mapping], initiatives: Collection[str]) -> dict[str, int]:
    """Initiative id to its task count, a row with no `kind` being a task. Each id in `initiatives` carries an explicit 0, since a missing id reads as non-empty."""
    counts = collections.Counter(str(t["initiative"]) for t in tickets if t.get("kind", "task") == "task")
    return {**dict.fromkeys(initiatives, 0), **counts}


def _chair_git(args: list[str]) -> str | None:
    """Edge. Stdout of `git <args>`, or None on a non-zero exit, a timeout or a missing git."""
    try:
        done = subprocess.run(["git", *args], capture_output=True, text=True, timeout=5, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return done.stdout if done.returncode == 0 else None


class _CheckoutReports:
    """Owned by one chair process: which (repo, kind) checkout notes were already printed."""

    def __init__(self) -> None:
        self._seen: set[tuple[str, str]] = set()

    def first(self, repo: str, kind: str) -> bool:
        """True the first time (repo, kind) is asked, False every time after."""
        fresh = (repo, kind) not in self._seen
        self._seen.add((repo, kind))
        return fresh


def _checkout_note(repo: Path, run: Callable[[Path, Sequence[str]], tuple[int, str]]) -> tuple[str, str | None]:
    """Edge. Fetch origin's default branch, then sync the checkout: (kind, one-line report or None when silent)."""
    code, out = run(repo, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    default = out.strip().removeprefix("origin/") if code == 0 and out.strip() else "main"
    code, out = run(repo, ["fetch", "--quiet", "origin", f"+refs/heads/{default}:refs/remotes/origin/{default}"])
    if code != 0:
        reason = next((line.strip() for line in out.splitlines() if line.strip()), f"exit code {code}")
        return "fetch_failed", f"checkout not fast-forwarded: fetch of origin/{default} failed: {reason}"
    verdict, line = checkout_sync.sync_checkout(repo, default, run)
    if line is None:
        return verdict, None
    return ("fast_forward_failed" if verdict == "fast_forward" else verdict), line


def _sync_chair_checkouts(
    repos: Sequence[str], reports: _CheckoutReports, echo: Callable[[str], None],
    run: Callable[[Path, Sequence[str]], tuple[int, str]] | None = None,
) -> None:
    """Edge, once per repo per tick. Silent for fast_forward and already_current; any other note prints once per
    (repo, kind) for the life of `reports`. Nothing raised here leaves the tick."""
    runner = run or checkout_sync.git_runner
    for repo in dict.fromkeys(repos):
        try:
            kind, line = _checkout_note(Path(repo), runner)
        except Exception as exc:
            kind, line = "error", f"checkout sync of {repo} failed: {exc}"
        if line is not None and reports.first(repo, kind):
            with contextlib.suppress(Exception):
                echo(f"chair run: {repo}: {line}")


def _chair_float(table: Mapping[str, object], key: str, default: float) -> float:
    """`table[key]` when it is a number of zero or more (a bool is not one), else `default`; zero is a real setting."""
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return default
    return float(value)


def _chair_cache_paths(runs_dir: Path) -> tuple[Path, Path, Path]:
    """The work-file, store-row and decompose-log cache files; `save_cache` writes one cache per path."""
    return runs_dir / "chair-cache.json", runs_dir / "chair-cache-store.json", runs_dir / "chair-cache-decompose.json"


CHAIR_WAKE_POLL_SECONDS = 5.0  # how often the between-tick wait re-reads the bus for a run_exited entry

# Bounds of the process-lifetime read caches, sized from the work they hold so a day of ticks cannot grow memory.
CHAIR_FILES_CACHE_MAX = 4096  # one entry per work file: a few thousand
CHAIR_STORE_CACHE_MAX = 8  # one entry per store read shape: the work rows and the queue rows
CHAIR_RECORDS_CACHE_MAX = 4096  # one entry per run task record of an open task: a few thousand
CHAIR_ANCESTRY_CACHE_MAX = 4096  # git ancestry answers: a few land watches times the fifty green runs each
CHAIR_LANDED_CACHE_MAX = 200_000  # one verdict per run task record on disk: tens of thousands on a long-lived workspace
CHAIR_DECOMPOSE_CACHE_MAX = 256  # one intake's exit rows, task count and run logs: a few hundred runs at most
CHAIR_DECOMPOSE_INTAKES_MAX = 64  # intakes holding a decompose cache at once; the least recently used is dropped


def _takes_cache(read: Callable[..., list[dict]], cache: object) -> Callable[..., list[dict]]:
    """Edge. `read` with `cache=` bound when it has that parameter, else `read` itself, called as before."""
    try:
        takes = "cache" in inspect.signature(read).parameters
    except (TypeError, ValueError):
        takes = False
    return (lambda *args, **kwargs: read(*args, cache=cache, **kwargs)) if takes else read


def _chair_run_deps(
    runs_dir: Path, profile: dict, session: str, pid: int, host: str, dry_run: bool, echo: Callable[[str], None],
    profile_path: Path, mode: str, worker_epoch: Callable[[], int] | None = None,
    record_store: Callable[[list[str]], tuple[int, str]] | None = None,
) -> chair_run.RunDeps:
    """Every source has an edge reader; `_ChairUnwired` stays for a source that loses one. A dry run never beats the lease.

    `worker_epoch` builds the deps a queue worker (`cox chair worker`) gathers and performs with: its actions are fenced
    and recorded under its own kind lease's epoch, `beat` only clears the tick's read caches and never touches the
    chair's lease, and it starts no land sink, so it never collects the tick's land results. `record_store` replaces
    the store write of each action's row (the worker's is fenced on its kind lease)."""
    holder = chair.lease_holder(session, pid, host)
    ws = runs_dir.parent
    home = os.path.expanduser("~")
    harness_dir = profile.get("harness_dir", "")
    harness_python = str(install_root.harness_python(harness_dir)) if harness_dir else "python"

    def now() -> datetime.datetime:
        return datetime.datetime.now(datetime.UTC)

    def now_text() -> str:
        return now().strftime("%Y-%m-%dT%H:%M:%SZ")

    snapshot: list[dict] = []  # edge state: `beat` empties it, so one tick's three docket readers share one read
    warned_hosts: list[frozenset[str]] = [frozenset()]  # edge state: the hosts `beat` has already warned are overloaded

    def warn_overloaded_hosts() -> None:
        # A failed hosts read must not break the beat: warn nothing and keep the warned set.
        try:
            warned_hosts[0] = chair_load_warn.warn_overloaded(run_store.hosts(runs_dir), echo, warned_hosts[0])
        except (*run_store._DB_ERRORS, OSError, RuntimeError, ValueError, TypeError):
            pass

    runner = chair_revert_ports.bounded(chair_exec.run_argv)  # gh and git of the revert path never wait past the bound

    main_ci = _main_ci_reader(runner)  # edge state: `beat` clears its cache, one gh read per land per tick
    green_since = _green_since_reader(runner)  # edge state: `beat` clears its cache, one read per land per tick
    # edge state for the whole process: each open land PR's check bodies, kept a few minutes so a round's `gh` reads are mostly hits
    lands_checks = chair_read_idle_lands.cached_checks(chair_read_idle_lands.gh_checks, {})

    # edge state: `beat` empties it, so one tick's window, weekly and source readers share one meter read and verdict
    meter_snapshot: list[tuple[usage_meter.Meter | None, str]] = []

    # edge state: `beat` empties it, so the orchestration fallback notice prints once per tick
    fallback_noticed: list[bool] = []

    # edge state: `beat` empties it, so one tick reads each work file, the store's work rows, the attempts and the
    # stranded inputs once and every reader that needs one shares it
    tick_reads: dict[str, object] = {}

    def once(key: str, read: Callable[[], object]) -> object:
        if key not in tick_reads:
            tick_reads[key] = read()
        return tick_reads[key]

    # Process-lifetime, unlike `tick_reads`: each store reader's rows, reused while the store's change counters for the
    # tables it reads stand, so a quiet tick sends one counters query instead of a read per reader (H6).
    reader_cache = chair_tick_cache.ReaderCache(time.monotonic)

    def stamped(name: str, tables: tuple[str, ...], read: Callable[[], Any], key: Hashable = ()) -> Any:
        counters = once("store_counters", lambda: run_store.table_counters(runs_dir))  # one query a tick
        return reader_cache.get(name, tables, counters, read, key)  # type: ignore[arg-type]

    # One windowed chair_actions read per tick, shared by the gather, held() and resolve_land. The window reaches back
    # `chair.stale_days` at least; revert_watch rows are kept at any age by `_KEPT_KINDS`, so an open watch is never lost.
    actions_window = max(
        chair_read_stale.ACTIONS_WINDOW_DAYS, chair_facts.resolve_stale_days((profile.get("chair") or {}).get("stale_days"))
    )
    # Process-lifetime, unlike `tick_reads`: the window is kept and each tick reads only its newest rows (H6).
    actions_cache = chair_actions_cache.ActionsCache()
    actions = lambda: once(  # noqa: E731 -- the one reader `FactsDeps.actions` and the watch readers share
        "chair_actions", lambda: actions_cache.read(runs_dir, now(), actions_window)
    )

    # Process-lifetime, unlike `tick_reads`; `load_cache` fills them from the previous chair's stop.
    files_path, store_path, decompose_path = _chair_cache_paths(runs_dir)
    files_cache = chair_read_cache.StampCache(CHAIR_FILES_CACHE_MAX)
    store_cache = chair_read_cache.StampCache(CHAIR_STORE_CACHE_MAX)
    queue_cache = chair_read_cache.StampCache(CHAIR_STORE_CACHE_MAX)  # not saved: queue rows carry claims
    records_cache = chair_read_cache.StampCache(CHAIR_RECORDS_CACHE_MAX)  # not saved: the records are files a tick re-stamps
    if not dry_run:
        chair_read_cache.load_cache(files_cache, files_path)
        chair_read_cache.load_cache(store_cache, store_path)
    def postgres_store() -> bool:
        return is_postgres(run_store.store_url_for(runs_dir))

    # `read_decompose_runs` drops every entry its call did not touch, so each intake gets its own cache.
    decompose_caches: collections.OrderedDict[str, object] = collections.OrderedDict()

    def decompose_cache_for(intake_id: str):
        if intake_id not in decompose_caches:
            cache = chair_read_cache.StampCache(CHAIR_DECOMPOSE_CACHE_MAX)
            if not dry_run:
                chair_read_cache.load_cache(cache, decompose_path)  # the first call for this intake prunes what it does not use
            decompose_caches[intake_id] = cache
            while len(decompose_caches) > CHAIR_DECOMPOSE_INTAKES_MAX:
                decompose_caches.popitem(last=False)
        decompose_caches.move_to_end(intake_id)
        return decompose_caches[intake_id]

    def save_caches() -> None:
        chair_read_cache.save_cache(files_cache, files_path)
        chair_read_cache.save_cache(store_cache, store_path)
        if decompose_caches:  # none read this run: leave the saved file for the next chair
            merged = chair_read_cache.StampCache(CHAIR_DECOMPOSE_CACHE_MAX * len(decompose_caches))
            for cache in decompose_caches.values():
                chair_read_cache.load_entries(merged, chair_read_cache.dump_entries(cache))  # type: ignore[arg-type]
            chair_read_cache.save_cache(merged, decompose_path)

    # With the workspace out of git the store is the board, and the tick reads its work from the store's rows,
    # rendered as the files would be. It opens no work file, so it needs none to be there.
    rows_are_the_board = mode == "store" and not workspace_git_on(profile)

    def work_files() -> Mapping[Path, str]:
        if rows_are_the_board:
            return once("files", lambda: chair_read_quarantined.store_work_files(ws, run_store.read_queue(runs_dir)))  # type: ignore[return-value]
        return once("files", lambda: chair_read_quarantined.read_work_files(ws, files_cache))  # type: ignore[return-value]

    def store_rows() -> list:
        return once(  # type: ignore[return-value]
            "store_rows",
            lambda: stamped("work_items", ("work_items",), lambda: run_store.work_items(runs_dir, None, store_cache)) if mode == "store" else [],
        )

    def attempts() -> list[dict]:
        # Under work_state store nothing writes an attempt into a work file: the run task records under runs/ hold the
        # cause, run and reason of each open task's attempts, and a fetched remote run's records land there too.
        if mode == "store":
            return once(  # type: ignore[return-value]
                "attempts", lambda: chair_read_run_records.read_store_attempts(ws, work_files(), work_rows(), records_cache)
            )
        return once("attempts", lambda: chair_read_attempts.read_attempts(ws, work_files()))  # type: ignore[return-value]

    def stranded_inputs() -> tuple[list[dict], list[dict]]:
        return once("stranded", lambda: _chair_stranded_inputs(ws, mode, work_files(), store_rows()))  # type: ignore[return-value]

    def intake_fingerprint(path: str) -> str | None:
        # The tick's one queue read, shared by the facts and the exec; None when the store read gave no row for it.
        return chair_read_decompose_miss.fingerprint_or_none(ws, work_rows(), path, store_mode=mode == "store")

    def work_rows() -> list[dict]:
        # `store_rows` (run_store.work_items) carries no `kind` column; the queue read does. Store mode only, once per tick.
        return once(  # type: ignore[return-value]
            "work_rows",
            lambda: stamped("queue", ("work_items",), lambda: _takes_cache(run_store.read_queue, queue_cache)(runs_dir)) if mode == "store" else [],
        )

    def ticket_items() -> list[dict]:
        return once("tickets", lambda: chair_read_docket.docket_work_items(ws, mode, work_files(), store_rows()))  # type: ignore[return-value]

    def decompose_runs_of(path: str, initiative: str = "") -> list:
        iid = chair_exec.decompose_id(path, chair_exec.read_intake_id(ws, path))
        if isinstance(iid, chair_exec.Refusal):
            return []
        # The launch decomposes under a fresh id when a dropped initiative holds the intake id. The dropped one asking
        # for itself (`initiative == iid`) keeps its own runs.
        under = iid if initiative == iid else _decompose_initiative_id(iid, work_rows())
        # A SQLite store's file stamp keeps each intake's runs across ticks; a Postgres store has none, so every intake
        # shares the tick's one read instead of sending its own two queries.
        tick = once("decompose_tick_runs", lambda: chair_decompose_runs.read_tick_runs(Path(runs_dir))) if postgres_store() else None
        # C5: a `collision` step row of the initiative counts against the decompose that wrote its tasks.
        collided = once("decompose_collisions", lambda: chair_decompose_runs.read_collisions(Path(runs_dir)))
        runs = chair_decompose_runs.read_decompose_runs(Path(runs_dir), under, decompose_cache_for(under), tick)  # type: ignore[arg-type]
        return chair_decompose_runs.collided_runs(runs, collided, under)  # type: ignore[arg-type]

    def initiative_intakes() -> dict[str, str]:
        return _initiative_intakes(chair_read_quarantined.initiative_files(ws, work_files()))

    def item_counts() -> dict[str, int]:
        # An initiative naming an intake is `decomposed` (or `landed`, which holds items) by `intake_state.intake_group`.
        return _task_item_counts(ticket_items(), initiative_intakes())

    def initiative_states() -> dict[str, str]:
        # The store row of an initiative is keyed by its own id with no phase; the rows are the tick's cached read.
        return {r["initiative"]: r["state"] for r in store_rows() if r["task_id"] == r["initiative"] and not r["phase"]}

    def decomposed_intake() -> dict[str, str]:
        # Only the initiatives with zero task items can be reported by `empty_decompose_facts`, so only their decompose
        # runs are read: no run read for an initiative that has tasks. Keyed by the initiative's directory name, the key
        # tickets and live runs use; the run list is read under the intake's own decompose id, which may differ from it.
        intakes = initiative_intakes()
        empty = [initiative for initiative, count in item_counts().items() if count == 0]
        return _newest_run_ids({i: [r.run_id for r in decompose_runs_of(intakes[i], i)] for i in empty})

    def land_at_map() -> dict[str, str]:
        # Initiative id to its resolved land_at; an initiative with no value reads ticket.
        return once(  # type: ignore[return-value]
            "land_at",
            lambda: chair_facts.land_at_facts(
                _team_cartridge(profile)[0],
                _initiative_land_at(chair_read_quarantined.initiative_files(ws, work_files())),
                sorted({t["initiative"] for t in ticket_items()}),
            ),
        )

    def docket() -> dict:
        if not snapshot:
            busy = chair_read_docket.busy_lane_count(ws / "runs", now_text())
            snapshot.append(
                chair_facts.docket_by_land_at(ticket_items(), busy, _local_max_in_flight(runs_dir, profile, host), land_at_map())
            )
        return snapshot[0]

    def beat() -> object:
        snapshot.clear()
        tick_reads.clear()
        main_ci.cache.clear()
        green_since.cache.clear()
        meter_snapshot.clear()
        fallback_noticed.clear()
        if worker_epoch is not None:  # a queue worker beats its own kind lease, in its own loop
            warn_overloaded_hosts()
            return ""
        lost = "" if dry_run else chair.renew_lease(runs_dir, session, pid, host)
        if _record_beat_wanted(dry_run, lost):
            _beat_record(runs_dir, session, pid, host, now())
        warn_overloaded_hosts()
        return lost

    def live_initiatives() -> list[str]:
        return chair_read_live.read_live_initiatives(runs_dir, [row["id"] for row in docket()["initiatives"]], now_text())

    def watch_rows() -> list:
        """The land watch and verdict rows of the tick's actions, which the watch facts read instead of every row."""
        return once("watch_rows", lambda: chair_revert_watch.watch_rows(actions()))  # type: ignore[return-value]

    # Process-lifetime: each run task record's landed verdict, kept by the record's file stamp.
    landed_cache = chair_read_cache.StampCache(CHAIR_LANDED_CACHE_MAX)

    def landed_ids() -> frozenset[str]:
        """The landed task ids `approved` and `phase_tasks` both read, once a tick."""
        return once("landed_ids", lambda: chair_read_approved.read_landed_task_ids(ws / "runs", landed_cache))  # type: ignore[return-value]

    def approved() -> list:
        # The land's runs come from `land_records`: a code or ticket cause hides a record from the stranded report,
        # but an approved item still lands from the run holding its approval.
        records, items = stranded_inputs()
        unsettled = chair_read_approved.read_unsettled_runs(runs_dir, records)
        stranded = chair_read_stranded.read_stranded(chair_read_approved.land_records(records, unsettled), items)
        facts = chair_read_approved.read_fetch_facts(runs_dir, stranded)
        return chair_read_approved.with_runs(
            chair_read_approved.read_approved(ws, mode, work_files(), store_rows(), landed_ids()), stranded, facts,
        )

    last_missing_repos: list[str] = []  # edge state: the current tick's `missing_repos()` result, read back by `reported_repos()`

    def missing_repos() -> list[str]:
        last_missing_repos[:] = chair_read_stranded.read_missing_repos(*stranded_inputs())
        return last_missing_repos

    def reported_repos() -> set[str]:
        """Returns the set reported as of the *previous* tick, then persists this tick's `missing_repos()`
        as the new reported set, so a still-missing repo is not reported again and one that reappears
        as present is dropped. A dry run never persists, matching `beat`'s own rule."""
        previous = _read_reported_repos(runs_dir)
        if not dry_run:
            _write_reported_repos(runs_dir, last_missing_repos)
        return previous

    def epoch() -> int:
        if worker_epoch is not None:
            return worker_epoch()
        lease = chair._read_lease(runs_dir, holder)
        return lease["epoch"] if lease is not None else -1  # no lease matches no action's epoch, so all are fenced

    record = chair_read_record.recorder(
        runs_dir, epoch, now_text, store=record_store or store_cli.runner(runs_dir), holder=holder
    )

    def has_patch(initiative: str, task: str) -> bool:
        """`read_has_patch` needs a run id: the newest attempt on this task names it. No attempt has no record."""
        run = chair_read_patch.newest_runs(attempts()).get((initiative, task))
        return run is not None and chair_read_patch.read_has_patch(runs_dir, str(run), task)

    profile_text = _read_text_or_none(profile_path) or ""  # the `profile` dict never carries lane_hosts
    parsed_hosts = _profile_lane_hosts(profile_text)
    if isinstance(parsed_hosts, lane_hosts.LaneHostError):
        echo(f"chair run: lane_hosts ignored: {parsed_hosts.message}")
    startup_rows = run_store.hosts(runs_dir)
    startup_hosts = _lane_host_candidates(profile_text, startup_rows)
    unreachable = [] if isinstance(startup_hosts, lane_hosts.LaneHostError) else host_cmd.dispatchable(startup_hosts)[1]
    # The local row is never truly unreachable: its own workspace_dir is the routing profile's, resolved here
    # instead of refused even when its own `cox host beat` never recorded one.
    local_resolved = host_cmd.local_workspace_dir(startup_rows, host, str(profile.get("workspace_dir", "")))
    unreachable = [name for name in unreachable if not (name == host and local_resolved)]
    if unreachable:
        echo(f"chair run: not dispatching to {', '.join(unreachable)}: no workspace_dir; run `cox host beat` on each")
    shadow = _shadowed_note(profile_text, [str(r["name"]) for r in startup_rows])
    if shadow:
        echo(f"chair run: {shadow}")
    if not dry_run and host_cmd.local_host_missing(startup_rows, host):
        register_argv = host_cmd.local_host_add_argv(host, _chair_max_in_flight(runs_dir, profile), session)
        register_code, register_output = store_cli.runner(runs_dir)(register_argv)
        if register_code != 0:
            # A write failure (missing harness interpreter, unreachable store) must not stop the tick: the
            # loop falls back to the cartridge cap exactly as it does when the table carries no local row.
            echo(f"chair run: could not register this host in the table: store_cli exit {register_code}: {register_output.strip()}")

    def dispatch(row: dict) -> chair_facts.DispatchFacts:
        stamp = now_text()
        pidfile_live, pidfiled = chair_read_docket.local_runs(runs_dir, stamp)
        lanes = run_store.remote_lanes(run_store.live_lanes(runs_dir, stamp), pidfiled)
        host_rows = stamped("hosts", ("hosts",), lambda: run_store.hosts(runs_dir))  # a drain moves the stamp: no restart needed
        candidates = _lane_host_candidates(profile_text, host_rows, host)
        # a host with no workspace_dir is refused by `route launch --on`, so the loop must not plan launches onto it
        host_names = [] if isinstance(candidates, lane_hosts.LaneHostError) else host_cmd.dispatchable(candidates)[0]
        configured = chair_capacity.chair_local_lanes(profile)
        lane_gate = chair_capacity.chair_lane_prereqs(profile)
        effective = chair_orchestrate.effective_local_lanes(configured, host_names)  # no usable host: build locally this tick
        if configured == "decompose" and effective == "any" and not fallback_noticed:
            fallback_noticed.append(True)
            echo(f"chair run: {chair_orchestrate.FALLBACK_NOTICE}")
        return chair_facts.dispatch_facts(
            row, host_names, _dispatch_counts(lanes, host, host_names, pidfile_live), host_cmd.row_capacities(host_rows),
            weight=host_cmd.row_weights(host_rows), capabilities=host_cmd.row_capabilities(host_rows), local=host,
            local_lanes=effective, lane_prereqs=lane_gate,
        )

    weekly_reset = usage_window.parse_weekly_reset(profile.get("weekly_reset"))

    def meter_fresh_now() -> tuple[usage_meter.Meter | None, str]:
        """One meter read and one source verdict per tick, shared by `window`, `weekly` and both source
        labels, so a label always names the side its figure was built from."""
        if not meter_snapshot:
            chosen = _chair_meter(usage_meter.read(), now(), runs_dir)
            _reconcile_ceilings(*chosen, runs_dir)  # once per tick, before `window` or `weekly` reads a ceiling
            meter_snapshot.append(chosen)
        return meter_snapshot[0]

    def window() -> pacing.Window:
        meter, source = meter_fresh_now()
        now_ = now()
        if meter is not None and meter.five_hour is None:
            return usage_meter.unlimited_window(now_)  # no five-hour limit on this plan; the label still names the meter
        ceiling = usage_meter.implied_ceiling("five_hour", now_)
        if ceiling is None:
            ceiling = profile.get("window_ceiling_usd")
        estimate_window = usage_window.gather(runs_dir, now_, ceiling_usd=ceiling)
        usable = (
            usage_meter.usable_percentage("five_hour", meter.five_hour, meter.observed_at, now_, Path(runs_dir), ceiling)
            if meter is not None else None
        )
        meter_window = (
            usage_meter.as_window(
                dataclasses.replace(meter.five_hour, used_percentage=usable), now_, datetime.timedelta(hours=5),
            )
            if usable is not None else None
        )
        if source == "meter":
            usage_meter.record_implied_ceiling("five_hour", meter.five_hour, Path(runs_dir), now_)
        return usage_meter.prefer(meter_window, estimate_window)

    def window_source() -> str:
        return meter_fresh_now()[1]

    def weekly() -> pacing.Window:
        meter, source = meter_fresh_now()
        now_ = now()
        ceiling = usage_meter.implied_ceiling("weekly", now_)
        if ceiling is None:
            ceiling = profile.get("weekly_ceiling_usd")
        estimate_weekly = usage_window.gather_weekly(
            runs_dir, now_, ceiling,
            store_spend=lambda since: run_store.cost_since(runs_dir, since),
            reset=weekly_reset,
        )
        usable = (
            usage_meter.usable_percentage("weekly", meter.seven_day, meter.observed_at, now_, Path(runs_dir), ceiling)
            if meter is not None else None
        )
        meter_weekly = (
            usage_meter.as_window(
                dataclasses.replace(meter.seven_day, used_percentage=usable), now_, datetime.timedelta(days=7),
            )
            if usable is not None else None
        )
        if source == "meter":
            usage_meter.record_implied_ceiling("weekly", meter.seven_day, Path(runs_dir), now_)
        return usage_meter.prefer(meter_weekly, estimate_weekly)

    def weekly_source() -> str:
        return meter_fresh_now()[1]

    def round_intake() -> Sequence[str]:
        return once(  # type: ignore[return-value]
            "intake", lambda: chair_read_intake.read_intake(ws, chair_read_quarantined.initiative_files(ws, work_files()))
        )

    def round_live_initiatives() -> list[str]:
        return once("live_initiatives", live_initiatives)  # type: ignore[return-value]

    def idle_live(probe: Sequence[str]) -> list[str]:
        """Live ids among `probe`: the round's docket read answers for its initiatives, only the rest are probed."""
        known = {row["id"] for row in docket()["initiatives"]}
        wanted = set(probe)
        extra = sorted(wanted - known)
        found = wanted & set(round_live_initiatives())
        return sorted(found | set(chair_read_live.read_live_initiatives(runs_dir, extra, now_text()) if extra else []))

    def idle_stall(n: datetime.datetime) -> chair_facts.IdleStallInputs:
        return chair_facts.idle_stall_inputs(
            chair_read_idle_backlog.read_idle_backlog(
                ws, mode, _local_max_in_flight(runs_dir, profile, host), n, work_files(), store_rows(), actions(),
                queued=round_intake(), items=ticket_items(), live=idle_live, land_at=land_at_map(),
            ),
            chair_read_idle_hosts.read_idle_hosts(runs_dir, startup_hosts),
            chair_read_idle_lands.read_lands_waiting(runs_dir, n, lands_checks),
            chair_read_idle_open.read_idle_open(ws, actions()),
            chair_facts.resolve_idle_stall_minutes((profile.get("chair") or {}).get("idle_stall_minutes")),
        )

    sources_configured, profile_stale = chair_read_intake.sources_configured_keeping_last_good(profile_path)
    # C3: one reader of the worker leases for the facts, the pre-perform re-read and the per-action check
    queue_leases = chair_read_queues.QueueLeasesReader(runs_dir, time.monotonic)
    repo_for = _repo_for(ws, home)
    lane_capacity = _check_capacity(runs_dir, profile, host)

    def wrongly_released() -> list:
        files, at = work_files(), land_at_map()
        bodies = [  # the work items carry frontmatter fields only, so the note is read from the file text
            {**i, "body": route.parse_frontmatter(files.get(ws / "work" / str(i["initiative"]) / str(i["file"]), ""))[1]}
            for i in ticket_items() if i.get("state") == "ready"
        ]
        merged = _wrongly_released_merged(
            _approved_branch_merged(runner, repo_for, runs_dir), runs_dir, lambda: _live_runs(runs_dir, now_text()), at
        )
        return chair_facts.wrongly_released_facts(bodies, merged, at)

    facts_deps = chair_facts.FactsDeps(
        approved_branch_merged=_approved_branch_merged(runner, repo_for, runs_dir),
        wrongly_released=wrongly_released,
        lease=lambda: chair_read_lease.read_lease(runs_dir, now()),
        window=window,
        weekly=weekly,
        window_source=window_source,
        weekly_source=weekly_source,
        policy=lambda: _resolved_pacing_policy(runs_dir),
        month=lambda: _profile_month(runs_dir, profile, now(), _resolved_pacing_policy(runs_dir).min_elapsed_fraction),
        docket=docket,
        approved=approved,
        phase_tasks=lambda: chair_read_approved.read_phase_tasks(ws, mode, work_files(), store_rows(), landed_ids()),
        phase_records=lambda tasks: chair_read_phase_records.read_phase_records(runs_dir, tasks),
        phase_queue=lambda: chair_read_phase_queue.read_phase_queue(runs_dir, profile, work_rows()) if mode == "store" else None,
        quarantined=lambda: chair_read_quarantined.read_quarantined(ws, mode, work_files(), store_rows()),
        held_files=lambda: queue_export.read_held_record(ws),
        stranded=lambda: chair_read_stranded.read_stranded(*stranded_inputs()),
        queue=work_rows if mode == "store" else None,
        stranded_records=(lambda: stranded_inputs()[0]) if mode == "store" else None,
        missing_repos=missing_repos,
        reported_repos=reported_repos,
        handled_keys=lambda: _read_handled_keys(runs_dir),
        run_exited=lambda: chair_read_exits.exits(runs_dir),
        attempts=attempts,
        has_patch=has_patch,
        live_initiatives=live_initiatives,
        intake=round_intake,
        work_store_ready=lambda: chair_read_docket.work_store_ready(docket()),
        sources_configured=sources_configured,
        profile_stale=profile_stale,
        chair_queues=chair_read_queues.queues_keeping_last_good(profile_path),
        queue_leases=queue_leases,
        session=session, pid=pid, host=host, dispatch=dispatch,
        drafts=lambda: draft_list.count_drafts(
            draft_list.read_drafts(ws / "work", now_text(), chair_read_quarantined.initiative_files(ws, work_files()))
        ),
        remote_unfetched=lambda: chair_read_remote_unfetched.read_remote_unfetched(
            runs_dir, [row["id"] for row in docket()["initiatives"]]
        ),
        lost_runs=lambda: chair_read_lost.read_lost_runs(runs_dir, now_text()),
        tickets=ticket_items,
        landing_records=lambda: _landing_records(runs_dir),
        decomposed_intake=decomposed_intake,
        item_counts=item_counts,
        initiative_states=initiative_states,
        work_rows=work_rows,
        repos=lambda: _initiative_repos(chair_read_quarantined.initiative_files(ws, work_files()), home),
        cartridge=lambda: _team_cartridge(profile)[0],  # {} when unreadable: land_at then reads the default
        land_at_values=lambda: _initiative_land_at(chair_read_quarantined.initiative_files(ws, work_files())),
        actions=actions,
        land_watches=lambda: chair_revert_watch.pending_watches(watch_rows),
        read_main_ci=main_ci,
        green_since=green_since,
        land_hold=lambda: chair_smoke.read_hold(str(runs_dir)),
        land_outcomes=lambda: chair_revert_watch.outcomes(watch_rows),
        decompose_runs=decompose_runs_of,
        decompose_cutoff=lambda path: chair_decompose_runs.read_released_at(ws, path),
        intake_fingerprint=intake_fingerprint,
        newest_run_host=lambda: stamped(
            "newest_run_hosts", ("runs",),
            lambda: run_store.newest_run_hosts(runs_dir, [row["id"] for row in docket()["initiatives"]]),
            key=tuple(row["id"] for row in docket()["initiatives"]),
        ),
        history=lambda: chair_read_housekeeping.read_last_housekeeping(runs_dir),
        housekeeping_hours=lambda: (profile.get("chair") or {}).get("housekeeping_hours"),
        stale_days=lambda: (profile.get("chair") or {}).get("stale_days"),
        max_launches_per_hour=lambda: (profile.get("chair") or {}).get("max_launches_per_hour"),
        quarantine_hold_after=lambda: (profile.get("chair") or {}).get("quarantine_hold_after"),
        no_progress_after=lambda: (profile.get("chair") or {}).get("no_progress_after"),
        stall_alarm=lambda: chair_stall_alarm.profile_settings(profile.get("chair")),
        # The runs, phases and task records live in the store; with no store the reader finds no runs and holds nothing.
        run_outcomes=lambda ids, last: chair_read_run_outcomes.read_run_outcomes(runs_dir, ids, last),
        merge_queue=lambda: profile.get("merge_queue"),
        shared_file_limit=lambda: (profile.get("chair") or {}).get("max_shared_file_initiatives"),
        serial_files=lambda: profile.get("serial_files"),
        host_load_ceiling=lambda: (profile.get("chair") or {}).get("host_load_ceiling"),
        # A run task record that names no body cannot be told apart from the current body, so the stale count leaves it out.
        stale_candidates=lambda n: chair_read_stale.read_stale_candidates(
            ws, n, work_files(), chair_read_run_records.stale_attempts(attempts()) if mode == "store" else attempts(), actions()
        ),
        stall_candidates=lambda n: chair_read_stall.read_stall_candidates(runs_dir, startup_hosts, n),
        artifacts_missing=lambda n: chair_read_artifacts.read_marked_steps(runs_dir, n),
        idle_stall=idle_stall,
        hosts=lambda: stamped("hosts", ("hosts",), lambda: run_store.hosts(runs_dir)),
        pid_probe=lambda: chair_pid_probe.read_pid_probe(
            runs_dir, now_text(), chair_exec.fenced_fetch(_pid_probe_ssh, dry_run), () if isinstance(parsed_hosts, lane_hosts.LaneHostError) else parsed_hosts, host
        ),
    )
    exec_deps = chair_exec.edge_deps(
        runs_dir, ws, session, pid,
        run_id=chair_read_run_id.make_run_id(runs_dir), repo_for=repo_for,
        record=record,
        host=host,
        harness_python=harness_python,
        log_retention_days=run_logs.retention_days(profile),
        ids_mode=profile.get("ids", "slug"),
        dry_run=dry_run,
        lane_capacity=lane_capacity,
    )
    exec_deps = dataclasses.replace(
        exec_deps,
        label=session,
        **_revert_land_deps(runs_dir, holder, runner, actions, record),
        rerun_main=lambda repo, commit: chair_revert_ports.rerun_infra(runner, _gh_repo(repo), commit),
        launch_stagger_s=chair_exec.stagger_seconds((profile.get("chair") or {}).get("launch_stagger_s")),
        mark_handled=None if dry_run else (lambda key: _mark_handled_key(runs_dir, key)),
        # The same reader the facts use, so a miss records the fingerprint the next tick's facts compare against.
        intake_fingerprint=intake_fingerprint,
        # C3, C4: the tick asks before each action of a queue kind whether a queue worker now holds that kind. A
        # worker's own deps ask nothing (its fence is its lease).
        queue_held=None if dry_run or worker_epoch is not None else queue_leases.held,
    )
    def held() -> None:
        """Resolves each pending watch the smoke hold does not name, when its commit is green on main or main went green after it."""
        for w in chair_revert_watch.pending_watches(actions):
            (ci, _) = main_ci(w["repo"], w["commit"])
            hold = chair_smoke.read_hold(str(runs_dir))
            unheld = hold is None or hold["land"]["commit"] != w["commit"]
            if unheld and (ci == "green" or green_since(w["repo"], w["commit"])):
                chair_revert_watch.resolve_watch(record, w["commit"], "held")

    stop_lands = chair_run.no_lands_to_stop
    lands_in_flight = chair_run.no_lands_in_flight
    if not dry_run and worker_epoch is None:
        # Lands run behind the tick on the land worker (tools #1287), which beats only the chair lease while one runs:
        # the tick's own `beat` also clears this tick's fact caches, which a worker thread must never touch.
        sink = chair_run.land_sink(lambda: chair.renew_lease(runs_dir, session, pid, host), released=lambda: chair.honour_release_request(runs_dir, session, pid, host))
        from agent_tools import chair_land_sink

        def spawn_land(argv: list[str]) -> int:
            return subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
            ).pid

        lands = chair_land_sink.ProcessLands(
            runs_dir / "chair-lands", sink,
            lambda action, code, output: chair_exec.land_outcome(action, exec_deps, code, output),
            land_limit.repo_land_limit, spawn_land, chair.pid_alive, host, time.time,
        )
        exec_deps = dataclasses.replace(exec_deps, lands=lands)
        facts_deps = dataclasses.replace(facts_deps, landing=lands.landing)
        stop_lands = sink.stop  # the thread sink only: a land process is never signalled
        # Land processes outlive the chair and a new chair adopts them from the state dir; only thread lands die with it.
        lands_in_flight = lambda: bool(lands.held_here())  # noqa: E731
    # A dry run never releases a real lease, so it passes no release check.
    release_requested = None if dry_run else (lambda: chair.honour_release_request(runs_dir, session, pid, host))
    checkout_state = lambda: chair_restart.read_checkout_state(chair_restart.checkout_root(), _chair_git)  # noqa: E731
    checkout_reports = _CheckoutReports()  # edge state for the whole process: each note prints once per repo
    changed_files = lambda old, new: chair_restart.changed_paths(chair_restart.checkout_root(), old, new, _chair_git)  # noqa: E731
    chair_table = profile.get("chair") or {}
    restart_path = chair_restart_log.record_path(runs_dir)

    def record_restart(reason: str) -> None:
        previous = chair_restart_log.read_record(restart_path)
        chair_restart_log.write_record(restart_path, chair_restart_log.bump(previous, reason, now()))

    restart_note = "" if dry_run else chair_restart_log.restart_note(chair_restart_log.read_record(restart_path), now())

    git_workspace = workspace_git_on(profile)

    def pull_and_sync() -> str:
        """The live tick's pull (when the workspace is in git), then the checkout sync of each repo the initiatives
        name; the sync never raises."""
        try:
            return workspace_sync.pull_workspace(ws) if git_workspace else ""
        finally:
            try:
                repos = _initiative_repos(chair_read_quarantined.initiative_files(ws, work_files()), home).values()
                _sync_chair_checkouts([r for r in repos if Path(r).is_dir()], checkout_reports, echo)
            except Exception as exc:
                if checkout_reports.first("", "repos"):
                    with contextlib.suppress(Exception):
                        echo(f"chair run: checkout sync skipped: {exc}")

    def sync_lane_checkouts_now() -> list[dict]:
        """The needs_chair actions for lane host checkouts left alone; the repos are the initiatives' that exist here."""
        repos = _initiative_repos(chair_read_quarantined.initiative_files(ws, work_files()), home).values()
        return lane_checkout_sync.sync_lane_checkouts(parsed_hosts, [r for r in repos if Path(r).is_dir()], str(ws), _lane_sync_ssh)

    has_lane_hosts = not isinstance(parsed_hosts, lane_hosts.LaneHostError) and bool(parsed_hosts)

    started_checkout = checkout_state()
    dev_install = started_checkout is not None and started_checkout.on_branch  # the condition that arms the restart rule
    towpath = shutil.which("towpath")
    dash_wiring = {
        "dash_dir": str(chair_restart.checkout_root().parent / "coxswain-dash"),
        "bin_dir": os.path.dirname(towpath) if towpath else os.path.join(home, ".local", "bin"),
        "toolchain_found": shutil.which("cargo") is not None,
    } if dev_install else {}

    def read_bus() -> list[dict]:
        return courier.entries(courier.read_text(ws / "courier.jsonl"))  # a missing file is an empty bus

    # A dry run and a missing workspace keep the plain sleep, so polling is unchanged there.
    nap = _store_nap(runs_dir) if mode == "store" and not dry_run else None
    wake = None if dry_run or not ws.is_dir() else chair_wake.wait_for_run_exit(
        read_bus, time.sleep, time.monotonic, CHAIR_WAKE_POLL_SECONDS, nap)

    chat = None if dry_run or not ws.is_dir() else _chat_deps(ws)

    return chair_run.RunDeps(
        chat=chat,
        wait=wake,
        checkout_state=checkout_state, started_checkout=started_checkout, lands_in_flight=lands_in_flight,
        stop_lands=stop_lands, facts_deps=facts_deps, gather=_chair_gather(ws), exec_deps=exec_deps, report_deps=chair_report.Deps(echo=echo),
        beat=beat, current_epoch=epoch,
        lease_beat=lambda: chair.renew_lease(runs_dir, session, pid, host),
        holds=lambda: chair._read_lease(runs_dir, holder) is not None,
        release=lambda: chair.release_lease(runs_dir, session, pid, host), release_requested=release_requested,
        sleep=time.sleep, now=now,
        perform=_chair_perform_with_smoke(
            runs_dir, chair_exec.perform,
            watch=lambda w: chair_revert_watch.record_watch(record, w), held=held,
            tickets=lambda: chair_facts.read_ticket_items(ws, mode), echo=echo, **dash_wiring,
        ),
        export_rows=(lambda ids: _chair_export_hook(runs_dir, ids, git_workspace)) if mode == "store" and not dry_run else None,
        notify_config=notify_core.config_from_profile(profile), workspace=ws,
        pull_workspace=None if dry_run else pull_and_sync,
        lane_checkout_actions=sync_lane_checkouts_now if has_lane_hosts and not dry_run else None,
        changed_files=changed_files,
        restart_window_s=_chair_float(chair_table, "restart_window_s", 120.0),
        restart_max_defer_s=_chair_float(chair_table, "restart_max_defer_s", 600.0),
        restart_note=restart_note,
        record_restart=None if dry_run else record_restart,
        save_cache=None if dry_run else save_caches,
        store_reads=lambda: chair_tick_cache.merged_counts(reader_cache.tick_counts(), actions_cache.tick_counts()),
        queue_leases=None if dry_run else queue_leases,
    )


def _landing_records(runs_dir: Path) -> list[dict]:
    """Edge. The store's landing records for the chair's facts; empty, with a note, when they cannot be read."""
    from agent_tools import lander_ports
    return lander_ports.read_records(runs_dir, lambda line: print(line, file=sys.stderr))
