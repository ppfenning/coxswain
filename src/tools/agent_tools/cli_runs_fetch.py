"""`cox runs fetch`: bringing a run's branches and records back from a lane host or the store."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import yaml

from agent_tools.cli_common import _now_iso, _profile_path, _read_text_or_none
from agent_tools.cli_lake import _lake_provider
from agent_tools.cli_lazy import (
    bounded_run,
    chair_facts,
    host_cmd,
    land,
    lane_hosts,
    queue_export,
    remote_fetch,
    remote_lane,
    route,
    run_store,
    store_cli,
    work_state,
)


def _runs_dir_for_land(a: argparse.Namespace) -> tuple[Path | None, str | None]:
    """`--runs-dir` overrides; otherwise `runs/` under the profile's `workspace_dir`."""
    if a.runs_dir:
        return Path(a.runs_dir).expanduser(), None
    profile_path = _profile_path(a)
    text = _read_text_or_none(profile_path)
    if text is None:
        return None, f"no profile at {profile_path}"
    try:
        profile = route.parse_profile(text)
    except route.ProfileError as exc:
        return None, f"profile unreadable: {exc}"
    workspace = profile.get("workspace_dir", "")
    if not workspace:
        return None, f"workspace_dir not set in profile {profile_path}"
    return Path(workspace).expanduser() / "runs", None


def _initiative_of(work_root: Path, task_id: str) -> str | None:
    """The one initiative whose `work/<initiative>/<phase>/*.md` holds an item
    with this id (`id` falls back to the file stem, as `route.work_item` does),
    or `None` when none or several do. The task record never names it."""
    found = set()
    for p in work_root.glob("*/*/*.md"):
        text = _read_text_or_none(p)
        if text is None:
            continue
        item = route.work_item(route.parse_frontmatter(text)[0], initiative=p.parent.parent.name, phase_dir=p.parent.name, stem=p.stem)
        if item["id"] == task_id:
            found.add(item["initiative"])
    return found.pop() if len(found) == 1 else None


def _branch_exists(repo: Path, branch: str) -> bool:
    r = bounded_run.completed(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", branch], capture_output=True, text=True)
    return r.returncode == 0


_UNLABELED = "unlabeled"


def _holder_label(a: argparse.Namespace) -> str:
    return getattr(a, "label", None) or os.environ.get("COX_SESSION_LABEL") or _UNLABELED


def _remote_edge(cwd: Path) -> tuple[Callable[[list[str]], int], Callable[[str], str] | None]:
    """Edge: the one seam that runs rsync and ssh for `--on`; `cwd` is the workspace the `work/<id>` copy is relative to."""

    def run(argv: list[str]) -> int:
        try:
            return bounded_run.completed(argv, bounded_run.STEP_TIMEOUT_SECONDS, cwd=cwd).returncode
        except OSError as exc:
            print(f"{argv[0]}: {exc}")
            return 127

    return run, None


def _lane_host_candidates(
    profile_text: str, rows: Sequence[Mapping], local: str = "",
) -> tuple[lane_hosts.LaneHost, ...] | lane_hosts.LaneHostError:
    """The table's active hosts when the table has rows, else the profile's `lane_hosts`. A row named `local` is
    never one of them: the loop's own machine is never a remote lane-host candidate for itself."""
    parsed = _profile_lane_hosts(profile_text)
    if not rows:
        return parsed
    return host_cmd.host_rows_to_lane_hosts(rows, () if isinstance(parsed, lane_hosts.LaneHostError) else parsed, local)


def _remote_fetch_facts(runs_dir: Path, run: str) -> tuple[bool, str | None]:
    """Edge. (lease_released, ended_at) for `run` from the store, as the docket reads them: a live lease, or no ended span, keeps a lane unfetched."""
    live = {lane.run for lane in run_store.live_lanes(runs_dir, _now_iso())}
    ended = {span_run: ended_at for span_run, _, ended_at in run_store.run_spans(runs_dir, "")}
    return run not in live, ended.get(run)


def _remote_capture(cwd: Path) -> Callable[[list[str]], str | None]:
    """Edge: runs an argv and returns its stdout, None when it cannot run or exits non-zero."""

    def capture(argv: list[str]) -> str | None:
        try:
            done = bounded_run.completed(argv, cwd=cwd, capture_output=True, text=True)
        except OSError:
            return None
        return done.stdout if done.returncode == 0 else None

    return capture


def _task_pairs_from_listing(text: str) -> list[tuple[str, str]]:
    """Pure: (phase, task) for each `<phase>/<task>.json` file in an `rsync -r --list-only` listing of a run's `tasks/`."""
    fields = (line.split(None, 4) for line in text.splitlines())
    paths = (f[4].split("/") for f in fields if len(f) == 5 and f[0].startswith("-"))
    return [(path[0], path[1].removesuffix(".json")) for path in paths if len(path) == 2 and path[1].endswith(".json")]


def _store_holds_every_record(pairs: list[tuple[str, str]], read: Callable[[str, str], dict | None]) -> bool:
    """Pure: True when `read` finds a record for every (phase, task) pair; no pairs is False."""
    held = {task for phase, task in pairs if read(phase, task) is not None}
    return remote_lane.records_already_in_store([task for _, task in pairs], held)


def _store_task_records(runs_dir: Path, run_id: str, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], dict | None]:
    """Edge: each pair's task record from the store; a pair the store cannot read maps to None."""

    def read(phase: str, task: str) -> dict | None:
        try:
            return run_store.task_record(runs_dir, run_id, phase, task)
        except (OSError, ValueError):
            return None

    return {(phase, task): read(phase, task) for phase, task in pairs}


def _branch_only_repos(
    runs_dir: Path, host: lane_hosts.LaneHost, locate: Callable[[str], str] | None, run_id: str, recorded: list[str],
) -> list[str] | None:
    """Edge. The repos to fetch branches for when the store holds every task record of the remote run, else None.

    The records are not copied, so the repos come from the store records, then from the remote record. With neither the
    branch fetch has nowhere to go, and the caller copies the records as before."""
    place = locate if locate is not None else (lambda path: f"{host.ssh}:{path}")
    tasks_dir = f"{host.workspace_dir.rstrip('/')}/runs/{run_id}/tasks/"
    listing = _remote_capture(runs_dir.parent)(["rsync", "-r", "--list-only", place(tasks_dir)])
    pairs = _task_pairs_from_listing(listing or "")
    records = _store_task_records(runs_dir, run_id, pairs)
    complete = _store_holds_every_record(pairs, lambda phase, task: records[(phase, task)])
    if remote_lane.fetch_scope(complete) != "branches":
        return None
    stored = [r["repo"] for r in records.values() if r is not None and isinstance(r.get("repo"), str) and r["repo"]]
    return list(dict.fromkeys(stored or recorded)) or None


def _store_run_rows(runs_dir: Path, run_id: str, wanted: set[str] | None = None) -> dict[tuple[str, str], dict | None]:
    """Edge. The run's `task_records` rows from the store alone, keyed (phase, task); the remote is never listed.
    `wanted` limits the read to those task ids. A row the store cannot read is left out."""
    if wanted is not None and not wanted:
        return {}
    tasks = [task for task in run_store.run_task_ids(runs_dir, run_id) if wanted is None or task in wanted]
    pairs = [(phase, task) for task in tasks for phase in run_store.task_record_phases(runs_dir, run_id, task)]
    return {pair: record for pair, record in _store_task_records(runs_dir, run_id, pairs).items() if record is not None}


def _store_held_runs(runs_dir: Path) -> list[str]:
    """Edge. Fetched remote runs: a store-mode fetch leaves their records in the store, sometimes beside a local record
    file for one task, so a local file does not make the run's other tasks local. A landed-elsewhere marker names a run
    with nothing left to land, so it is not one."""
    suffix = ".fetched.json"
    fetched = [p.name[: -len(suffix)] for p in sorted(runs_dir.glob(f"*{suffix}"))]
    return [
        run for run in fetched
        if not remote_lane.is_landed_elsewhere_marker(_read_text_or_none(remote_lane.fetched_record_path(runs_dir, run)))
    ]


def _local_record_tasks(runs_dir: Path, run: str) -> set[str]:
    """Edge. The task ids that have a record file under `runs/<run>/tasks/`; those are read from the file, not the store."""
    return {path.stem for path in (runs_dir / run / "tasks").glob("*/*.json")}


def _store_held_records(runs_dir: Path, items: list[dict]) -> list[dict]:
    """Edge. The store's task records of every `_store_held_runs` run, for tasks whose item is ready or approved,
    each carrying `run`, `task` and `phase` as a record read from `runs/<run>/tasks/` does."""
    live = {(item["initiative"], item["id"]) for item in items if item.get("state") in ("ready", "approved")}
    return [
        {"run": run, "task": task, "phase": phase, **record}
        for run in _store_held_runs(runs_dir)
        for (phase, task), record in _store_run_rows(
            runs_dir, run,
            {task for initiative, task in live if initiative == chair_facts.run_initiative(run)} - _local_record_tasks(runs_dir, run),
        ).items()
        if record is not None
    ]


def _approved_store_rows(records: Mapping[tuple[str, str], dict | None]) -> list[tuple[str, str, dict]]:
    """Pure: (phase, task, record) for every store row whose review and arbitration verdicts approve it
    (`land._approved` is None); a row the store could not read is left out."""
    return [
        (phase, task, record)
        for (phase, task), record in records.items()
        if record is not None and land._approved(record) is None
    ]


def _store_approved_task_facts(
    runs_dir: Path, work_root: Path, run_id: str, records: Mapping[tuple[str, str], dict | None], by: str,
) -> list[dict]:
    """Edge. One approved-task fact per store row that is approved and whose work item moves from ready to
    approved; each fact carries `run = run_id` so a `land_phase` planned from it names a run. A row whose
    initiative cannot be found, or whose compare-and-set the store refuses, contributes no fact."""
    facts = []
    started = run_store.run_started_at(runs_dir, run_id)
    for phase, task, _record in _approved_store_rows(records):
        initiative = _initiative_of(work_root, task)
        if initiative is None:
            continue
        row = next((r for r in run_store.work_items(runs_dir, initiative) if r.get("task_id") == task), None)
        if row is not None and remote_fetch.changed_after_start(row.get("updated_at"), started):
            print(f"fetch: {task} kept {row.get('state')}, changed after run {run_id} started")
            continue
        moved = land.set_state_stop(store_cli.set_state(runs_dir, initiative, task, "approved", by, expected="ready")) is None
        if moved:
            facts.append({"id": task, "initiative": initiative, "phase": phase, "run": run_id})
    return facts


def _landed_elsewhere(
    runs_dir: Path, host: lane_hosts.LaneHost, locate: Callable[[str], str] | None, run_id: str, ended: bool,
) -> bool:
    """Edge. `remote_lane.landed_elsewhere` for the run: the store is read only once it has ended, and the remote is
    listed only once every stored task is done. A task whose initiative cannot be found counts as not done, and a
    listing that fails is passed on as None, so the caller fetches as before."""
    work_root = runs_dir.parent / "work"

    def state(task: str) -> str | None:
        initiative = _initiative_of(work_root, task)
        return None if initiative is None else run_store.task_state_of(runs_dir, initiative, task)

    states = {task: state(task) for task in run_store.run_task_ids(runs_dir, run_id)} if ended else {}
    place = locate if locate is not None else (lambda path: f"{host.ssh}:{path}")
    tasks_dir = f"{host.workspace_dir.rstrip('/')}/runs/{run_id}/tasks/"
    listing = _remote_capture(runs_dir.parent)(["rsync", "-r", "--list-only", place(tasks_dir)]) if remote_lane.all_landed(states) else None
    listed = None if listing is None else [task for _, task in _task_pairs_from_listing(listing)]
    return remote_lane.landed_elsewhere(states, listed, ended)


def _fetch_one(runs_dir: Path, hosts, run_id: str, mode: str = "files", by: str = _UNLABELED) -> tuple[str, list[str], str]:
    """Edge. (outcome, lines, host name) for one remote run; outcome is fetched, live or failed.

    The task records are always copied, their host paths rewritten to this machine's, and the marker lists them.
    Under `mode` "store", when the store already holds every one of them, the
    approved ones move their work item from ready to approved in the store instead, so an approved-task fact
    built from them carries this run. An ended run whose stored tasks are all done, and whose remote lists no
    task the store lacks, is marked fetched with nothing pulled. The remote is listed to check that, so an
    unreachable remote is fetched as before and fails there."""
    text = _read_text_or_none(remote_lane.remote_record_path(runs_dir, run_id))
    record = remote_lane.parse_remote_record(text) if text is not None else None
    if record is None:
        return "failed", [f"fetch: no {run_id}.remote.json in {runs_dir}; the run was not launched with --on"], ""
    if isinstance(hosts, lane_hosts.LaneHostError):
        return "failed", [f"fetch: {hosts.message}"], ""
    host = lane_hosts.find_lane_host(hosts, record["host"])
    if host is None:
        return "failed", [f"fetch: host {record['host']} is in neither the hosts table nor the profile lane_hosts",
                          f"configured: {', '.join(h.name for h in hosts) or 'none'}"], ""
    lease_released, ended_at = _remote_fetch_facts(runs_dir, run_id)
    run, locate = _remote_edge(runs_dir.parent)
    if remote_lane.is_decompose_record(record):
        # parse_remote_record keeps only the fields remote_record knows, so the initiative is read from the raw text.
        initiative = json.loads(text).get("initiative")
        return _fetch_decompose(runs_dir, host, initiative, run_id, lease_released, ended_at, run)
    ended = remote_fetch.refuse_unended(lease_released, ended_at) is None
    if mode == "store" and _landed_elsewhere(runs_dir, host, locate, run_id, ended):
        remote_lane.fetched_record_path(runs_dir, run_id).write_text(json.dumps(remote_lane.landed_elsewhere_marker(_now_iso())))
        return "fetched", [remote_lane.landed_elsewhere_line(run_id)], host.name
    recorded = [record["repo"]] if record.get("repo") else []
    only = _branch_only_repos(runs_dir, host, locate, run_id, recorded) if mode == "store" else None

    def repo_paths(run_dir: Path) -> list[str]:
        return (remote_fetch.task_repos(run_dir) or recorded) if only is None else only

    diverged: list[str] = []
    result = remote_fetch.fetch_run(host, run_id, runs_dir, repo_paths, run, locate,
                                    lease_released=lease_released, ended_at=ended_at, home=os.path.expanduser("~"),
                                    diverged=diverged)
    if isinstance(result, remote_fetch.FetchError):
        detail = f"{run_id} is still live on {host.name}: {result.message}" if result.step == "refuse" else result.message
        return ("live" if result.step == "refuse" else "failed"), [f"fetch: {result.step}: {detail}"], host.name
    wanted = remote_fetch.phase_branches(run_store.phase_manifests(runs_dir, run_id))
    brought = (remote_fetch.fetch_phase_branches(host, run_id, runs_dir, list(result), wanted, run, locate,
                                                 home=os.path.expanduser("~"), diverged=diverged)
               if wanted else ())
    # A diverged phase branch kept its local tip; the line names the side ref holding the lane's, so nothing is lost
    # silently. Printed here because `--all` drops a fetched run's lines, and the chair reads this output.
    for line in dict.fromkeys(diverged):
        print(f"{run_id}: {line}")
    marker = {"fetched_at": _now_iso(), "repos": list(result), "tasks": remote_fetch.task_record_ids(runs_dir / run_id),
              "phase_branches": list(brought)}
    remote_lane.fetched_record_path(runs_dir, run_id).write_text(json.dumps(marker))
    if only is not None:
        records = _store_run_rows(runs_dir, run_id)
        moved = _store_approved_task_facts(runs_dir, runs_dir.parent / "work", run_id, records, by)
        if moved:
            print(f"{run_id}: {len(moved)} task(s) approved from the store")
    return "fetched", [f"run {run_id}", f"host {host.name}", "\n".join(f"repo {repo}" for repo in result)], host.name


def _repo_restore(pulled: str, intake: str | None) -> tuple[str, str] | None:
    """Pure. (initiative text carrying the intake's repo, that repo), or None when the pulled text already has a repo or the intake gives none."""
    held = route.parse_frontmatter(pulled)[0].get("repo")
    found = route.parse_frontmatter(intake)[0].get("repo") if intake is not None else None
    if (isinstance(held, str) and held) or not (isinstance(found, str) and found):
        return None
    return route.initiative_with_repo(pulled, found), found


def _restore_intake_repo(workspace: Path, pulled: str) -> tuple[str, str]:
    """Edge. The staged initiative.md text with the intake's repo put on it, and that repo; the text unchanged and "" when nothing changed."""
    named = route.parse_frontmatter(pulled)[0].get("intake")
    intake = _read_text_or_none(workspace / named) if isinstance(named, str) and named else None
    restored = _repo_restore(pulled, intake)
    return (pulled, "") if restored is None else restored


def _store_initiative_repo(runs_dir: Path, initiative: str, repo: str) -> str:
    """Edge. Set `extra.repo` on the initiative's stored row when it differs; "" when done or nothing to do, else the store's refusal.
    The store fill leaves an existing row whose `extra` is not null alone, so a repo missing there is set here."""
    row = next((r for r in run_store.read_queue(runs_dir, initiative, kind="initiative") if r.get("task_id") == initiative), None)
    extra = (row or {}).get("extra") or {}
    if row is None or extra.get("repo") == repo:
        return ""
    return run_store.upsert_row_detail(runs_dir, {**{k: v for k, v in row.items() if k not in ("holder", "epoch", "expires_at")}, "extra": {**extra, "repo": repo}})


def _fetch_decompose(
    runs_dir: Path, host, initiative: object, run_id: str, lease_released: bool, ended_at: str | None,
    run: Callable[[list[str]], int],
) -> tuple[str, list[str], str]:
    """Edge. Fetch an ended remote decompose: its `work/<initiative>` files and log, rows first, files after.

    A decompose leaves files and a log, never a branch, so no git runs and no task path is rewritten. The files are
    staged under `runs/.fetch-staging/<run>/`, their rows are written to the store and read back, and only then are
    they copied into `work/` (recorded as written), the staging removed and the marker written. Any store failure
    returns "failed" with the staged files kept and no marker, so the next tick pulls and tries again."""
    from agent_tools import store_fill  # imported here: cli loads a command module only when it runs

    refusal = remote_fetch.refuse_unended(lease_released, ended_at)
    if refusal is not None:
        return "live", [f"fetch: refuse: {run_id} is still live on {host.name}: {refusal}"], host.name
    if not isinstance(initiative, str) or not initiative or Path(initiative).name != initiative:
        return "failed", [f"fetch: the {run_id} remote record names no initiative"], host.name
    workspace = runs_dir.parent
    staging = Path(remote_fetch.staging_dir(str(workspace), run_id))
    staged_work = staging / "work" / initiative
    staged_work.mkdir(parents=True, exist_ok=True)
    for argv in remote_fetch.decompose_pull_argvs(host.ssh, host.workspace_dir, str(workspace), initiative, run_id):
        code = run(argv)
        if code != 0:
            return "failed", [f"fetch: rsync: {' '.join(argv)} exited {code}"], host.name
    texts = {p.relative_to(staging).as_posix(): p.read_text(encoding="utf-8") for p in sorted(staged_work.rglob("*.md")) if p.is_file()}
    initiative_key = f"work/{initiative}/initiative.md"
    repo = ""
    if initiative_key in texts:
        texts[initiative_key], repo = _restore_intake_repo(workspace, texts[initiative_key])
    files = [(tuple(key.split("/")), text) for key, text in texts.items()]
    parsed, _ = store_fill.parse_workspace(files)
    confirmed = {key: text for key, text in texts.items() if store_fill.parse_workspace([(tuple(key.split("/")), text)])[0]}
    existing = store_fill._read_store(runs_dir)
    if existing is None:
        return "failed", [f"fetch: store: {store_fill._UNREADABLE}"], host.name
    for row in store_fill.plan_fill(parsed, existing).writes:
        detail = run_store.upsert_row_detail(runs_dir, row)
        if detail:
            return "failed", [f"fetch: store: store refused {row['initiative']}/{row['task_id']}: {detail}"], host.name
    detail = _store_initiative_repo(runs_dir, initiative, repo) if repo else ""
    if detail:
        return "failed", [f"fetch: store: {detail}"], host.name
    stored = store_fill._read_store(runs_dir)
    if stored is None:
        return "failed", [f"fetch: store: {store_fill._UNREADABLE}"], host.name
    missing = remote_fetch.unconfirmed_keys([(r["initiative"], r["task_id"]) for r in parsed], stored)
    if missing:
        return "failed", [f"fetch: store: rows not read back: {', '.join('/'.join(key) for key in missing)}"], host.name
    for key, text in confirmed.items():
        (workspace / key).parent.mkdir(parents=True, exist_ok=True)
        (workspace / key).write_text(text, encoding="utf-8")
    queue_export.record_written(workspace, confirmed)
    shutil.rmtree(staging)
    with contextlib.suppress(OSError):
        staging.parent.rmdir()  # runs/.fetch-staging goes with its last run
    tasks = [key.removeprefix(f"work/{initiative}/") for key in confirmed if key != initiative_key]
    marker = {"fetched_at": _now_iso(), "initiative": initiative, "tasks": tasks}
    remote_lane.fetched_record_path(runs_dir, run_id).write_text(json.dumps(marker))
    return "fetched", [f"run {run_id}", f"host {host.name}", f"initiative {initiative}"], host.name


def _land_refetch_phase_branch(a: argparse.Namespace, runs_dir: Path, run_id: str, repo: Path, branch: str) -> str | None:
    """Edge. The remote host name when `branch` is missing here and one more fetch of the run does not bring it, else None.

    A run with no remote record is never checked. The fetch runs in files mode so it moves nothing in the store."""
    text = _read_text_or_none(remote_lane.remote_record_path(runs_dir, run_id))
    record = remote_lane.parse_remote_record(text) if text is not None else None
    if record is None or _branch_exists(repo, f"refs/heads/{branch}"):
        return None
    hosts = _fetch_hosts(_read_text_or_none(_profile_path(a)) or "", run_store.hosts(runs_dir))
    _fetch_one(runs_dir, hosts, run_id, "files")
    return None if _branch_exists(repo, f"refs/heads/{branch}") else record["host"]


def _runs_fetch_all(runs_dir: Path, hosts, mode: str = "files", by: str = _UNLABELED) -> int:
    """Fetches every remote run with no `.fetched.json` marker; a live run is skipped, a failed fetch makes the exit 2."""
    suffix = ".remote.json"
    remote_runs = [p.name[: -len(suffix)] for p in sorted(runs_dir.glob(f"*{suffix}"))]
    fetched = {run for run in remote_runs if remote_lane.fetched_record_path(runs_dir, run).exists()}
    todo = remote_lane.unfetched(remote_runs, fetched)
    if not todo:
        print("nothing to fetch")
        return 0
    failed = False
    for run_id in todo:
        outcome, lines, host = _fetch_one(runs_dir, hosts, run_id, mode, by)
        if outcome == "live":
            print(f"{run_id}: still live on {host}")
        elif outcome == "fetched":
            marker = _read_text_or_none(remote_lane.fetched_record_path(runs_dir, run_id))
            print(remote_lane.landed_elsewhere_line(run_id) if remote_lane.is_landed_elsewhere_marker(marker) else f"{run_id}: fetched from {host}")
        else:
            failed = True
            print("\n".join([f"{run_id}: {lines[0]}", *lines[1:]]))
    return 2 if failed else 0


def _fetch_hosts(profile_text: str, rows: Sequence[Mapping]) -> tuple[lane_hosts.LaneHost, ...] | lane_hosts.LaneHostError:
    """The hosts a remote run may be fetched from: the same table-then-profile hosts `route launch --on` picks from,
    drained ones included, since a run launched before its host was drained still has to come back. Reading only
    the profile's lane_hosts refused every run launched on a hosts-table host (2026-10-23)."""
    return _lane_host_candidates(profile_text, [{**row, "state": "active"} for row in rows])


def _runs_fetch(a: argparse.Namespace) -> int:
    """Pulls an ended remote run's records, log and branches to this machine; writes nothing in the store."""
    if bool(a.run_id) == bool(a.all):
        print("fetch: give one run id, or --all")
        return 2
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"fetch: {reason}")
        return 2
    hosts = _fetch_hosts(_read_text_or_none(_profile_path(a)) or "", run_store.hosts(runs_dir))
    mode = work_state.work_state_mode(_lake_provider(a)[0])
    by = _holder_label(a)
    if a.all:
        return _runs_fetch_all(runs_dir, hosts, mode, by)
    outcome, lines, _ = _fetch_one(runs_dir, hosts, a.run_id, mode, by)
    print("\n".join(lines))
    return 0 if outcome == "fetched" else 2


def _profile_lane_hosts(text: str) -> tuple[lane_hosts.LaneHost, ...] | lane_hosts.LaneHostError:
    """Reads `lane_hosts` straight from the YAML text; `route.parse_profile` only skips the block."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return lane_hosts.LaneHostError(f"profile is not readable as YAML: {exc}")
    return lane_hosts.parse_lane_hosts(data if isinstance(data, Mapping) else {})
