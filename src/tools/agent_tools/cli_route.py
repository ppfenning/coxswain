"""`cox route`: the board's status, context, edits, holds and stages, and the `pr`, `epic` and `plan` commands."""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import getpass
import json
import os
import socket
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path

from agent_tools.cli import (
    _LAND_LOCK_POLL_SECONDS,
    _after_ids,
    _export_board,
    _gather_context,
    _heartbeats,
    _initiative_token,
    _intake_groups,
    _lake_provider,
    _lane_live,
    _mtime_iso,
    _note_export_stamp,
    _now_iso,
    _profile_path,
    _read_text_or_none,
    _resolved_gate_level,
    _run_short_ids,
    _run_slug,
    _runs_dir_for_land,
    _stored_work_items,
    _usage_assessment,
    _with_lane_fields,
    _with_remote_lanes,
    _work_items,
    chair_capacity,
    chair_orchestrate,
    draft_apply,
    draft_list,
    epic,
    intake_queue,
    plan,
    pr_land,
    queue_rows,
    release_gate,
    remote_lane,
    repo_path,
    repo_path_migrate,
    route,
    route_drift,
    route_edit,
    route_epic,
    route_guard,
    route_hold,
    route_import,
    route_remove,
    route_requeue,
    run_store,
    store_cli,
    work_state,
    workspace_git_on,
)


def _pr_land(a: argparse.Namespace) -> int:
    """Wait for the PR's required checks, squash-merge it and mark its tasks done; exit 2 when it was not merged."""
    runs_dir, reason = _runs_dir_for_land(argparse.Namespace(runs_dir=None, profile=None))
    deps = pr_land.real_deps(runs_dir or Path("."), a.timeout, _LAND_LOCK_POLL_SECONDS)
    if runs_dir is None:  # the merge still goes ahead; the carried tasks are named as not marked
        deps = dataclasses.replace(deps, mark_done=lambda task, url: (False, f"no runs dir: {reason}"))
    result = pr_land.land_pr(Path(a.repo).expanduser(), a.number, deps)
    print(f"{result.event}: {result.text}")
    return 0 if result.event == pr_land.LEVEL_MERGED else 2


def _epic_watch(a: argparse.Namespace) -> int:
    out = epic.watch(a.pidfile, log=a.log, max_seconds=a.max_seconds, interval=a.interval)
    print(json.dumps(out, indent=2) if a.json else "\n".join(
        [f"pid {out['pid']}: {'fenced' if out['state'] == 'fenced' else 'finished' if out['finished'] else 'still running'}",
         *out.get("quarantined", []), *out.get("reused", []), *(x for x in (out.get("summary"), out.get("usage")) if x)]))
    return 0 if out["finished"] else 3






def _plan_serve(a: argparse.Namespace) -> int:
    if a.check:
        print(plan.check(a.dir))
    print(plan.serve(a.dir, kind=a.kind, open_browser=not a.no_open)); return 0


def _route_context(a: argparse.Namespace) -> int:
    # The edge is allowed to catch everything _gather_context can raise: this
    # command must never take a session down with it (spec §2). That
    # tolerance is for profile/workspace file reads only — it stops at the
    # gatherer, per charter A6, so a bug in the usage assessment surfaces
    # instead of erasing an otherwise-good docket (run tools-pacing-7).
    try:
        # Inside the guard on purpose: the provider profile is a file read like the rest.
        provider_profile = _lake_provider(a)[0]
        work_mode, work_line = work_state.resolve(provider_profile)
        profile, reason, intake, runs, initiatives, problems, efficiency = _gather_context(_profile_path(a), work_mode)
    except Exception as exc:
        print(f"routing: context unavailable ({type(exc).__name__}: {exc})")
        return 0
    # Computed once via the gatherer, same reason `route launch` gates on;
    # only when there is a workspace to gather usage files from.
    usage_reason = (
        _usage_assessment(
            Path(profile["workspace_dir"]).expanduser() / "runs",
            profile.get("window_ceiling_usd"), profile.get("weekly_ceiling_usd"), profile.get("weekly_reset"),
            profile.get("monthly_budget_usd"), profile.get("budget_timezone"),
        ).reason
        if not reason and profile is not None else None
    )
    if a.json:
        doc = route.context_document(profile, intake, runs, initiatives, problems)
        if reason:
            doc["reason"] = reason
        if usage_reason is not None:
            doc["usage"] = usage_reason
        print(json.dumps(doc, indent=2))
    elif reason:
        # nothing was read, so print no counts: an unread workspace must not
        # look like an empty one
        empty_groups = {"queued": [], "decomposed": [], "landed": []}
        first_line = route.render_context(profile, empty_groups, [], []).partition("\n")[0]
        print(f"{first_line} ({reason})")
    else:
        gate_level = _resolved_gate_level(Path(profile["workspace_dir"]).expanduser() / "runs")
        rendered = route.render_context(profile, intake, runs, initiatives, problems, gate_level=gate_level, now=_now_iso(), efficiency=efficiency)
        # Printed whenever a provider profile was read. With none (`_lake_provider` gives `{}`)
        # there is no work-state source to name, and the output stays as it was before this line.
        if provider_profile:
            print(work_line)
        print(f"{rendered}\nusage: {usage_reason}")
    return 0


def _refuse_if_already_running(runs_dir: Path, prefix: str):
    """spec: at most one live run per `prefix` — the single-writer rule
    applied to a run-id prefix. Returns the refusal line to print, or
    None when no run under `prefix` has a live process. A fenced run
    answers to its pid but lost its lease, so it refuses too."""
    host = socket.gethostname()
    for run in epic.live_runs(runs_dir, prefix):
        if run["state"] == "fenced":
            return (
                f"routing: {run['run']} is fenced (pid {run['pid']} on {host}): its lease heartbeat stopped, "
                f"so the epoch fence refuses its writes. Stop pid {run['pid']} on {host} before relaunching"
            )
        return f"routing: {run['run']} is already running (pid {run['pid']}) on {host}"
    return None


def _intake_groups_for(ws: Path, items: list):
    """`_intake_groups` from disk, or `None` with no `intake/` dir."""
    return _intake_groups(ws, items) if (ws / "intake").is_dir() else None


def _status_rows_for(runs_dir: Path, short_ids: Mapping[str, str] | None = None) -> list:
    pids = {p.stem: t for p in sorted(runs_dir.glob("*.pid")) if (t := _read_text_or_none(p)) is not None}
    alive = {run_id: epic.run_live(route.parse_pid(t), runs_dir / f"{run_id}.pid") for run_id, t in pids.items()}
    fenced = {run_id: epic.run_state(route.parse_pid(t), runs_dir / f"{run_id}.pid") == "fenced" for run_id, t in pids.items()}
    started = {run_id: _mtime_iso(runs_dir / f"{run_id}.pid") for run_id in pids}
    runs = _with_remote_lanes(runs_dir, route.run_entries(pids, alive, started, _heartbeats(runs_dir, pids), fenced))
    summaries = {p.stem: epic.summarize_log(_read_text_or_none(p) or "") for p in runs_dir.glob("*.log")}
    entries = route.status_entries(runs, summaries)
    by_run = _run_short_ids([e["id"] for e in entries], short_ids or {})
    return _with_lane_fields(route.status_rows(entries, by_run), runs, _now_iso())


def _route_export(a: argparse.Namespace) -> int:
    """Edge. Refresh this machine's copy of the board from the store. No git, no lease."""
    profile = _courier_profile(a)
    if profile is None:
        return 2
    workspace = Path(profile["workspace_dir"]).expanduser()
    try:
        skipped = _export_board(workspace / "runs", workspace, prune_stale=not workspace_git_on(profile))
        _note_export_stamp(workspace / "runs", run_store.queue_stamp(workspace / "runs"))
    except Exception as exc:
        print(f"export: {type(exc).__name__}: {exc}")
        return 1
    print(f"export: {workspace} matches the store" + (f"; skipped {len(skipped)} contentless row(s)" if skipped else ""))
    return 0


def _route_cartridges(a: argparse.Namespace) -> int:
    """Edge. `publish` stores the profile's cartridges directory under its sha; `fetch` writes a stored snapshot there."""
    from agent_tools import cartridge_snapshot  # imported here: cli loads a command module only when it runs

    profile = _courier_profile(a)
    if profile is None:
        return 2
    folder = a.into or profile.get("cartridges_dir")
    if not folder:
        print("cartridges: the profile names no cartridges_dir; pass --into")
        return 2
    root, runs_dir = Path(folder).expanduser(), Path(profile["workspace_dir"]).expanduser() / "runs"
    try:
        if a.action == "publish":
            print(cartridge_snapshot.publish(runs_dir, root))
            return 0
        if not a.sha:
            print("cartridges: fetch needs --sha")
            return 2
        wrote, deleted = cartridge_snapshot.fetch(runs_dir, a.sha, root, replace=a.replace)
    except Exception as exc:
        print(f"cartridges: {type(exc).__name__}: {exc}")
        return 1
    print(f"cartridges: {root} holds {a.sha[:12]} ({wrote} written, {deleted} removed)")
    return 0


def _route_drift(a: argparse.Namespace) -> int:
    """Report only: exits 0 whether or not the store and the files disagree."""
    profile_path = _profile_path(a)
    text = _read_text_or_none(profile_path)
    if text is None:
        print(f"routing: no profile at {profile_path}")
        return 2
    try:
        profile = route.parse_profile(text)
    except route.ProfileError as exc:
        print(f"routing: profile unreadable: {exc}")
        return 2
    workspace = profile.get("workspace_dir", "")
    if not workspace:
        print(f"routing: workspace_dir not set in profile {profile_path}")
        return 2
    ws = Path(workspace).expanduser()
    rows = run_store.read_queue(ws / "runs")
    if not rows:
        print("store has no work_items")
        return 0
    items = _work_items(ws)
    files = [(item["initiative"], item["id"], item["state"]) for item in items]
    groups = _intake_groups_for(ws, items) or {}
    intake_files = [(entry["path"], name) for name, entries in groups.items() for entry in entries]
    task_rows = [r for r in rows if r.get("kind") == "task"]
    intake_rows = [r for r in rows if r.get("kind") == "intake"]
    found = route_drift.drift(files, task_rows, intake_files, intake_rows)
    print(route_drift.format_json(found) if a.json else route_drift.format_text(found))
    return 0


def _route_import_files(ws: Path) -> list[tuple[tuple[str, ...], str]]:
    """Edge: `(path_parts, text)` for every intake file, task file, and `initiative.md` under `ws`,
    `path_parts` relative to `ws`. `plan_import` still writes no row for an `initiative.md`; it reads
    the file's text only to derive an intake row's `queued`/`decomposed`/`landed` state."""
    paths = (
        sorted(ws.glob("intake/*.md"))
        + sorted(ws.glob("intake/done/*.md"))
        + sorted(ws.glob("work/*/*/*.md"))
        + sorted(ws.glob("work/*/initiative.md"))
    )
    return [(p.relative_to(ws).parts, t) for p in paths if (t := _read_text_or_none(p)) is not None]


def _route_import_workspace(a: argparse.Namespace) -> Path | str:
    """`--workspace`, else the profile's `workspace_dir`, else `.`, as `_sync_context` resolves it.
    A str is why the profile is unreadable."""
    if a.workspace:
        return Path(a.workspace).expanduser()
    text = _read_text_or_none(_profile_path(a))
    try:
        profile = route.parse_profile(text) if text is not None else {}
    except route.ProfileError as exc:
        return f"profile unreadable: {exc}"
    return Path(profile.get("workspace_dir") or ".").expanduser()


def _route_import(a: argparse.Namespace) -> int:
    """Loads intake/ and work/<initiative>/<phase>/*.md files into the store's work_items table.
    Idempotent: importing the same files again reports zero written. Exits 2, after printing the
    failing row's key and the store's error, then the count written so far, the moment a row fails
    to upsert."""
    ws = _route_import_workspace(a)
    if isinstance(ws, str):
        print(f"route import: {ws}")
        return 2
    # A workspace with neither dir is a wrong path, not an empty queue: refuse rather than report "written 0".
    if not (ws / "work").is_dir() and not (ws / "intake").is_dir():
        print(f"route import: no work/ or intake/ under {ws}; pass --workspace or set workspace_dir in the profile")
        return 2
    runs_dir = ws / "runs"
    plan = route_import.plan_import(_route_import_files(ws), run_store.read_queue(runs_dir))
    written = 0
    for row in plan.to_write:
        detail = run_store.upsert_row_detail(runs_dir, row)
        if detail:
            print(f"route import: store refused {row['initiative']}/{row['task_id']}: {detail}")
            print(route_import.format_summary(written, plan))
            return 2
        written += 1
    print(route_import.format_summary(written, plan))
    return 0


def _paths_lines(changes: Sequence[Mapping[str, str]]) -> list[str]:
    """One `key: old -> new` line per change, then the count."""
    return [f"{c['key']}: {c['old']} -> {c['new']}" for c in changes] + [f"{len(changes)} change(s)"]


def _open_files(files: Mapping[str, str]) -> dict[str, str]:
    """Pure. `files` without done or dropped records. An `initiative.md` is done when it says so or when route's rule
    does: it has task files and all are done or dropped. The file carries no state of its own, so its tasks decide."""
    parts = {rel: rel.split("/") for rel in files}
    states = {rel: route.parse_frontmatter(files[rel])[0].get("state") for rel, p in parts.items() if len(p) == 4 and p[0] == "work"}
    tasks = [{"initiative": parts[rel][1], "state": s} for rel, s in states.items() if s is not None]  # a stateless file is no task
    initiative_files = {rel: p[1] for rel, p in parts.items() if p[0] == "work" and p[2:] == ["initiative.md"]}
    finished = route.initiative_states(sorted(set(initiative_files.values())), tasks)
    return {
        rel: text
        for rel, text in files.items()
        if repo_path_migrate._file_is_open(text) and not finished.get(initiative_files.get(rel), False)
    }


def _open_rows(rows: Sequence[Mapping]) -> list[Mapping]:
    """Pure. `rows` without done or dropped records. An initiative row holds the default state, so it is done
    when route's rule says so of its task rows: it has tasks and all are done or dropped."""
    tasks = [{"initiative": r["initiative"], "state": r.get("state")} for r in rows if r.get("kind") == "task"]
    finished = route.initiative_states(sorted({r["initiative"] for r in rows if r.get("kind") == "initiative"}), tasks)
    return [
        r
        for r in rows
        if repo_path_migrate._is_open(r.get("state")) and not (r.get("kind") == "initiative" and finished[r["initiative"]])
    ]


def _paths_apply_rows(
    runs_dir: Path, rows: Sequence[Mapping], home: str, move: tuple[str, str] | None = None, open_only: bool = False
) -> int:
    """Edge. Upsert only `extra` and `surfaces` of each changed row, then export once; exit 1 on a refusal or failed export.
    The store owns state, needs and the claim columns, so a stale snapshot of them is never written back.
    With `open_only`, done and dropped rows are skipped."""
    refused = 0
    taken = 0
    for row in _open_rows(rows) if open_only else rows:
        migrated = repo_path_migrate.migrate_row(row, home, move=move)
        if migrated is None:
            continue
        partial = {k: migrated[k] for k in ("initiative", "task_id", "extra", "surfaces") if k in migrated}
        if run_store.upsert_row(runs_dir, partial):
            taken += 1
        else:
            refused += 1
            print(f"route paths: store refused {row.get('initiative')}/{row.get('task_id')}")
    if taken:
        try:
            _export_board(runs_dir, runs_dir.parent)
        except RuntimeError as exc:
            print(f"route paths: rows are migrated but the file export failed: {exc}")
            return 1
    return 1 if refused else 0


def _paths_apply_files(
    ws: Path, files: Mapping[str, str], home: str, move: tuple[str, str] | None = None, open_only: bool = False
) -> int:
    """Edge. Write each changed work file in place; with `open_only`, files of done and dropped records are left alone."""
    for rel, text in (_open_files(files) if open_only else files).items():
        migrated = repo_path_migrate.migrate_text(text, home, move=move)
        if migrated is not None:
            (ws / rel).write_text(migrated, encoding="utf-8")
    return 0


def _route_paths(a: argparse.Namespace) -> int:
    """Dry run unless `--apply`: lists each recorded repo path that would move to the tilde form.
    Store mode reads and upserts queue rows, then re-exports the files; file mode edits the files."""
    ws = _route_import_workspace(a)
    if isinstance(ws, str):
        print(f"route paths: {ws}")
        return 2
    if not (ws / "work").is_dir() and not (ws / "intake").is_dir():
        print(f"route paths: no work/ or intake/ under {ws}; pass --workspace or set workspace_dir in the profile")
        return 2
    store_mode = work_state.work_state_mode(_lake_provider(a)[0]) == "store"
    runs_dir, home = ws / "runs", os.path.expanduser("~")
    from agent_tools import store_fill  # imported here: cli loads a command module only when it runs

    rows = run_store.read_queue(runs_dir) if store_mode else []
    if store_mode and not rows and store_fill._read_store(runs_dir) is None:
        print(f"route paths: the store under {runs_dir} cannot be read; nothing was checked")
        return 2
    files = {} if store_mode else {"/".join(parts): text for parts, text in store_fill._workspace_files(ws)}
    move = (repo_path.contract(a.move[0], home), repo_path.contract(a.move[1], home)) if a.move else None
    open_only = move is not None
    if open_only:
        print("route paths: only open records are moved; done and dropped records are left alone")
    listed_rows, listed = (_open_rows(rows), _open_files(files)) if open_only else (rows, files)
    print("\n".join(_paths_lines(repo_path_migrate.plan(listed_rows, listed, home, move=move, open_only=open_only))))
    if not a.apply:
        return 0
    code = (
        _paths_apply_rows(runs_dir, rows, home, move, open_only)
        if store_mode
        else _paths_apply_files(ws, files, home, move, open_only)
    )
    if move is None:
        return code
    rows_after = run_store.read_queue(runs_dir) if store_mode else []
    files_after = {} if store_mode else {"/".join(parts): text for parts, text in store_fill._workspace_files(ws)}
    left_rows, left_files = (_open_rows(rows_after), _open_files(files_after)) if open_only else (rows_after, files_after)
    left = len(repo_path_migrate.plan(left_rows, left_files, home, move=move, open_only=open_only))
    print(f"route paths: {left} value(s) still under {a.move[0]}")
    return 1 if left else code


def _initiative_path(runs_dir: Path, token: str) -> Path:
    """Edge. A launch argument as a directory: a path stays as given, a bare short id becomes `<workspace>/work/<slug>`."""
    path = Path(token).expanduser()
    if len(path.parts) != 1 or path.exists():
        return path
    slug = _initiative_token(runs_dir, token)
    return path if slug == token else runs_dir.parent / "work" / slug


def _route_draft(a: argparse.Namespace, act: Callable[[Path, draft_apply.Store, str], int]) -> int:
    """Resolve the workspace and the work-state mode, then hand both and the initiative slug to `act`."""
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"route: {reason}")
        return 2
    store = draft_apply.Store(
        work_state.work_state_mode(_lake_provider(a)[0]),
        lambda initiative: run_store.work_items(runs_dir, initiative),
        lambda initiative, task, state, by, expected: store_cli.set_state(runs_dir, initiative, task, state, by, expected),
    )
    return act(runs_dir.parent / "work", store, _initiative_token(runs_dir, a.initiative))


def _route_approve(a: argparse.Namespace) -> int:
    return _route_draft(a, lambda work, store, slug: draft_apply.approve(work, slug, a.task, a.by, store))


def _route_decline(a: argparse.Namespace) -> int:
    return _route_draft(a, lambda work, store, slug: draft_apply.decline(work, slug, a.reason, a.by, store))


def _live_runs(runs_dir: Path, now: str) -> set[str]:
    """Edge. Every live run on any host: the store's live leases, which remote lanes hold, plus local pidfile lanes.

    A lane carries no initiative, so `route edit` and `route remove` both narrow the set with `_live_runs_on` to the
    runs recorded on the target initiative. A run id says nothing reliable about its initiative (`--run-id` takes any name).
    `now` is `_now_iso()`'s form, which `live_lanes` compares as text."""
    leased = {lane.run for lane in run_store.live_lanes(runs_dir, now)}
    return leased | {pf.stem for pf in runs_dir.glob("*.pid") if _lane_live(runs_dir, pf.stem)}


def _recorded_initiatives(runs_dir: Path, runs: Collection[str]) -> dict[str, str | None]:
    """Edge. Run to the initiative its `<run>.launched.json` (local) or `<run>.remote.json` (`--on`) names, None for none.

    A run with no such file, an unreadable one, or one without an `initiative` key is left out: its initiative is unknown.
    Runs launched before the key existed are in that case."""
    def read(run: str) -> dict:
        for path in (runs_dir / f"{run}.launched.json", remote_lane.remote_record_path(runs_dir, run)):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(doc, dict) and "initiative" in doc and isinstance(doc["initiative"], str | None):
                return doc
        return {}
    docs = {run: read(run) for run in runs}
    return {run: doc["initiative"] for run, doc in docs.items() if "initiative" in doc}


def _live_runs_on(live: Collection[str], initiative: str, recorded: Mapping[str, str | None]) -> set[str]:
    """Live runs that may be on `initiative`: every one except a run whose record names another initiative or none.

    A run absent from `recorded` is unknown and stays, so removal refuses until it is stopped."""
    return {run for run in live if run not in recorded or recorded[run] == initiative}


def _held_entries(rows: Sequence[Mapping]) -> list[dict]:
    """`render_status`'s `held` rows from queue rows: the intake id and its `hold_reason` when it has one."""
    return [{"id": row["task_id"], "reason": str(row["extra"].get("hold_reason") or "")} for row in rows]


def _waiting_by_entry_id(queue: Sequence[Mapping], waiting: Mapping[str, Sequence[str]]) -> dict[str, Sequence[str]]:
    """`waiting` (keyed by file stem) re-keyed by the `id:` the intake group lines print, which differs from the stem under slug ids."""
    ids = {row["task_id"]: str(row["extra"].get("id") or row["task_id"]) for row in queue if row["kind"] == "intake"}
    return {ids.get(task_id, task_id): names for task_id, names in waiting.items()}


def _route_edit(a: argparse.Namespace) -> int:
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"route: {reason}")
        return 2
    now = _now_iso()
    live = _live_runs(runs_dir, now)
    target_id = _initiative_token(runs_dir, a.id)
    on_target = _live_runs_on(live, target_id, _recorded_initiatives(runs_dir, live))
    result = route_edit.apply_edit(
        runs_dir, runs_dir.parent, target_id, title=a.title, body=a.body, body_file=a.body_file, repo=a.repo,
        surfaces=a.surfaces, state=a.state, dry_run=a.dry_run, live_runs=on_target, now=now,
        rebuild=getattr(a, "rebuild", False), force=getattr(a, "force", False), pr=getattr(a, "pr", None),
        link=getattr(a, "link", None), after=_after_ids(getattr(a, "after", None)), epic=getattr(a, "epic", None),
        note=getattr(a, "note", None), initiative_live=live, initiative=getattr(a, "initiative", None),
    )
    if result.code != route_edit.DONE:
        print(f"edit: {'refused' if result.code == route_edit.REFUSED else 'failed'}: {result.text}")
    else:
        print(result.text.rstrip("\n") or "edit: no change")
    return result.code


def _route_epic(a: argparse.Namespace) -> int:
    if a.action != "new":
        print(f"epic: unknown action {a.action!r}; usage: route epic new <id> [--title T] [--target-branch B] [--dry-run]")
        return 2
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"route: {reason}")
        return 2
    code, text = route_epic.create_epic(runs_dir.parent, a.id, a.title, a.target_branch, a.dry_run)
    print(text.rstrip("\n"))
    return code


def _route_remove(a: argparse.Namespace) -> int:
    if not a.id and a.match is None:
        print("remove: give an id or --match")
        return 2
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"route: {reason}")
        return 2
    store = draft_apply.Store(
        work_state.work_state_mode(_lake_provider(a)[0]),
        lambda initiative: run_store.work_items(runs_dir, initiative),
        lambda initiative, task, state, by, expected: store_cli.set_state(runs_dir, initiative, task, state, by, expected),
    )
    now, rows = datetime.datetime.now(datetime.UTC), run_store.read_queue(runs_dir)
    live = _live_runs(runs_dir, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    recorded = _recorded_initiatives(runs_dir, live)
    by = a.by or getpass.getuser()
    if len(a.id) == 1 and a.match is None:
        target_id = _initiative_token(runs_dir, a.id[0])
        rc = route_remove.remove(runs_dir.parent, target_id, a.reason, by, now, rows, _live_runs_on(live, target_id, recorded), store, dry_run=a.dry_run)
        target = route_guard.resolve_target(rows, target_id)
        if rc == route_guard.DONE and not a.dry_run and isinstance(target, route_guard.QueuedIntake):
            _record_intake_done(runs_dir, runs_dir.parent / "intake" / "done" / f"{target.task_id}.md")
        return rc
    ids = [_initiative_token(runs_dir, token) for token in a.id]
    targets, _why = route_remove.expand_targets(rows, ids, a.match)
    live_on = {target: _live_runs_on(live, target, recorded) for target in targets}
    resolved = [route_guard.resolve_target(rows, target) for target in targets]
    intakes = [t.task_id for t in resolved if isinstance(t, route_guard.QueuedIntake)]
    ws = runs_dir.parent
    rc = route_remove.remove_many(ws, ids, a.reason, by, now, rows, live_on, store, match=a.match, dry_run=a.dry_run)
    if not a.dry_run:
        for task_id in intakes:
            done = ws / "intake" / "done" / f"{task_id}.md"
            if done.is_file() and not (ws / "intake" / f"{task_id}.md").exists():
                _record_intake_done(runs_dir, done)
    return rc


def _route_hold(a: argparse.Namespace) -> int:
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"route: {reason}")
        return 2
    return route_hold.hold(runs_dir.parent, runs_dir, a.id, a.reason, a.by or getpass.getuser(), datetime.datetime.now(datetime.UTC), run_store.read_queue(runs_dir), dry_run=a.dry_run)


def _route_release(a: argparse.Namespace) -> int:
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"route: {reason}")
        return 2
    return route_hold.release(
        runs_dir.parent, runs_dir, a.id, run_store.read_queue(runs_dir), initiative_live=_live_runs(runs_dir, _now_iso()),
        dry_run=a.dry_run, now=datetime.datetime.now(datetime.UTC),
    )


def _route_set_stage(a: argparse.Namespace, stage: str) -> int:
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"route: {reason}")
        return 2
    return route_hold.set_stage(runs_dir.parent, runs_dir, a.id, stage, run_store.read_queue(runs_dir), dry_run=a.dry_run)


def _route_stage_ready(a: argparse.Namespace) -> int:
    return _route_set_stage(a, "ready")


def _route_stage_draft(a: argparse.Namespace) -> int:
    return _route_set_stage(a, "draft")


def _route_requeue(a: argparse.Namespace) -> int:
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"route: {reason}")
        return 2
    code, message = route_requeue.requeue(
        runs_dir.parent, runs_dir, a.id, dry_run=a.dry_run, now=datetime.datetime.now(datetime.UTC)
    )
    print(message)
    return code


def _record_intake_done(runs_dir: Path, done_path: Path) -> None:
    """Edge. Move a removed intake's queue row from `queued` to `done`, so `route status` and the guard stop seeing it.

    The file has already moved; a store that cannot take the row is one warning, as in `route.write_filed_item`."""
    row = queue_rows.parse_item("intake", ("done", done_path.name), done_path.read_text(encoding="utf-8"))
    if row is None or not run_store.upsert_row(runs_dir, row):
        print(f"remove: warning: the store still lists {done_path.stem} as queued; run cox route import to reconcile")


def _chair_machine_line(profile: Mapping, runs_dir: Path) -> str:
    """Edge. The status line naming this machine and its effective orchestration mode; empty when either read fails
    or the hosts table has no rows (`run_store.hosts` answers `[]` for no store, so there is nothing to report)."""
    try:
        rows = run_store.hosts(runs_dir)
        if not rows:
            return ""
        configured = chair_capacity.chair_local_lanes(dict(profile))
        return route.chair_machine_line(chair_orchestrate.mode_line(configured, rows, socket.gethostname(), _now_iso()))
    except Exception:
        return ""


def _initiative_events(queue: Sequence[Mapping], run_ids: Sequence[str]) -> dict[str, list]:
    """Run id to the `events` its initiative's own row carries. A run id is a slug with an optional
    `-<digits>` suffix, so it is looked up whole, then by `_run_slug`. A run with no events, or with
    anything but a list of them, is absent."""
    found = {
        str(r.get("initiative") or r.get("task_id") or ""): (r["extra"].get("events") if isinstance(r.get("extra"), Mapping) else None)
        for r in queue
        if r.get("kind") == "initiative"
    }
    by_run = {run_id: found.get(run_id) or found.get(_run_slug(run_id)) for run_id in run_ids}
    return {run_id: list(evs) for run_id, evs in by_run.items() if isinstance(evs, list) and evs}


def _route_status(a: argparse.Namespace) -> int:
    profile_path = _profile_path(a)
    text = _read_text_or_none(profile_path)
    if text is None:
        print(f"routing: no profile at {profile_path}")
        return 2
    try:
        profile = route.parse_profile(text)
    except route.ProfileError as exc:
        print(f"routing: profile unreadable: {exc}")
        return 2
    workspace = profile.get("workspace_dir", "")
    if not workspace:
        print(f"routing: workspace_dir not set in profile {profile_path}")
        return 2
    # Past this point a stray pidfile — hand-edited, absurdly large — must
    # not take the session down with it (spec §2); charter A6 puts
    # exceptions at the edge, matching _route_context's guard above.
    try:
        ws = Path(workspace).expanduser()
        provider_profile = _lake_provider(a)[0]
        work_mode, work_line = work_state.resolve(provider_profile)
        short_ids = run_store.initiative_short_ids(ws / "runs")
        rows = _status_rows_for(ws / "runs", short_ids)
        items = _stored_work_items(ws, work_mode)
        groups = _intake_groups_for(ws, items)
        problems = route.state_problems(items)
        queue = run_store.read_queue(ws / "runs")
        from agent_tools.version_info import package_version  # local: keeps the import off every other command's path

        held_intakes = intake_queue.held_rows(queue)
        waiting = intake_queue.waiting_on(queue)
        held = [
            *_held_entries(held_intakes),
            *route.held_initiative_entries(queue, release_gate.held_initiatives(queue), held_intakes),
        ]
        deferred = route.deferred_by_target(queue, package_version())
        if a.json:
            # The bare rows list is the shape older callers read; it stays when there
            # is nothing else to say. A problem is never dropped for lack of an intake dir.
            queue_facts = {
                **({"waiting": waiting, "held": held} if waiting or held else {}),
                **({"deferred": deferred} if deferred else {}),
            }
            if groups is None and not problems and not queue_facts:
                doc = rows
            elif groups is None:
                doc = {"runs": rows, "problems": problems, **queue_facts}
            else:
                doc = {"runs": rows, "intake": groups, "problems": problems, **queue_facts}
            print(json.dumps(doc, indent=2))
        else:
            shown, hidden = (rows, 0) if a.all else route.recent_rows(rows, datetime.datetime.now(datetime.UTC))
            status = route.render_status(
                shown, groups, problems, gate_level=_resolved_gate_level(ws / "runs"), hidden=hidden,
                short_ids=_run_short_ids([r["id"] for r in rows], short_ids),
                waiting=_waiting_by_entry_id(queue, waiting), held=held, deferred=deferred,
                chair_line=_chair_machine_line(profile, ws / "runs"),
                events=_initiative_events(queue, [r["id"] for r in rows]),
            )
            drafts = route.render_drafts(
                draft_list.read_drafts(ws / "work", datetime.datetime.now(datetime.UTC).isoformat()), draft_list.format_age
            )
            # Printed whenever a provider profile was read. With none (`_lake_provider` gives `{}`)
            # there is no work-state source to name, and the output stays as it was before this line.
            if provider_profile:
                print(work_line)
            print("\n".join(part for part in (status, drafts) if part))
    except Exception as exc:
        print(f"routing: status unavailable ({type(exc).__name__}: {exc})")
    return 0


def _courier_profile(a: argparse.Namespace) -> dict | None:
    """The parsed profile when it names a workspace; None, after saying why, when it does not."""
    profile, reason, *_ = _gather_context(_profile_path(a))
    if reason: print(f"routing: {reason}"); return None
    return profile
