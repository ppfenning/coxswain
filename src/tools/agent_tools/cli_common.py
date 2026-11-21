"""Helpers several cox command groups share: the profile path, the work items and intake groups, the store refusal and the board files."""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from agent_tools.cli import _STORE_FREE_COMMANDS
from agent_tools.cli_lazy import (
    chair_decompose_runs,
    completions,
    dash_history_store,
    epic,
    queue_export,
    route,
    run_store,
    store_dialect,
    store_url,
)

DEFAULT_PROFILE = "~/.config/agent-tools/profile.yaml"


def _profile_path(a: argparse.Namespace) -> Path:
    return Path(getattr(a, "profile", None) or os.environ.get("AGENT_TOOLS_PROFILE") or DEFAULT_PROFILE).expanduser()


def _read_text_or_none(p: Path):
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def _mtime_iso(p: Path):
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return None
    return datetime.datetime.fromtimestamp(mtime, tz=datetime.UTC).isoformat()


def _heartbeats(runs_dir: Path, run_ids) -> dict:
    """Edge. `run id -> heartbeat_at` for each run that holds its own store lease."""
    leases = {run_id: run_store.lease(runs_dir, run_id) for run_id in run_ids}
    return {run_id: row[2] for run_id, row in leases.items() if row is not None and row[0] == run_id}


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _with_remote_lanes(runs_dir: Path, local_entries: list) -> list:
    """Edge. The local run entries, then an entry for each live store lease no local pidfile names.

    `now` must be `_now_iso()`'s form: `live_lanes` compares lease times as text."""
    lanes = run_store.live_lanes(runs_dir, _now_iso())
    remote = run_store.remote_lanes(lanes, {entry["id"] for entry in local_entries})
    return [*local_entries, *route.remote_lane_entries(remote)]


def _with_lane_fields(rows: list, runs: list, now: str) -> list:
    """`status_rows` output with `host` and `remote` on every row. A remote row reads `alive` and carries its lane text as `summary`, because `status_rows` has no place for either."""
    by_id = {run["id"]: run for run in runs}

    def one(row: dict) -> dict:
        run = by_id.get(row["id"], {})
        if not run.get("remote"):
            return {**row, "host": run.get("host"), "remote": False}
        lane = route._lane(run, now)
        return {**row, "state": "alive", "host": run["host"], "remote": True, "summary": lane.removeprefix(run["id"] + " ")}

    return [one(row) for row in rows]


def _efficiency_figures(runs_dir: Path, now: str) -> dict | None:
    """Edge. Today's (UTC day of `now`) cost, landed count, completions, cost per completion and first-try share; None
    with no store or one that fails to answer.

    The one place both the docket's `efficiency:` line and the dash feed's `spend.today` read from. `landed` counts the
    tasks the land log shows reaching `mark_done` today. `completions` counts tasks the way the feed's spend_split
    windows do (`completions.day_completions`): a task done, or approved and merged into its phase or PR branch, or
    landed on main, at its earliest such stamp, if that falls today; a logged land the store holds no record of counts
    too. `cost_per_completion` is today's spend over `completions`, and `per_landed` is its deprecated alias, the same
    value, kept one release. The first-try share is still measured over the land log's tasks."""
    today = now[:10]
    day = datetime.date.fromisoformat(today)
    start = datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.UTC)
    tomorrow = (day + datetime.timedelta(days=1)).isoformat()
    try:
        spend = run_store.cost_since(runs_dir, today, tomorrow)
        if spend is None:
            return None
        landed = route.landed_today(_read_text_or_none(runs_dir / "land.jsonl") or "", today)
        counts = run_store.build_counts(runs_dir, [t for _, t in landed if t is not None], [r for r, _ in landed if r is not None])
        records = dash_history_store.read_task_records_since(runs_dir, today)
    except (*run_store._DB_ERRORS, OSError, RuntimeError, ValueError, TypeError):
        return None
    rate, measured = route.first_try(landed, counts)
    done = completions.day_completions(records, landed, start, start + datetime.timedelta(days=1))
    per_completion = spend / len(done) if done else None
    return {
        "date": today,
        "cost": spend,
        "landed": len(landed),
        "per_landed": per_completion,  # deprecated alias of cost_per_completion, the same value
        "completions": len(done),
        "cost_per_completion": per_completion,
        "first_try_rate": rate,
        "measured": measured,
    }


def _efficiency(runs_dir: Path, now: str) -> str | None:
    """Edge. The `efficiency:` line for the UTC day of `now`; None with no store or one that fails to answer."""
    figures = _efficiency_figures(runs_dir, now)
    if figures is None:
        return None
    return route.efficiency_line(figures["cost"], figures["completions"], figures["first_try_rate"], figures["measured"])


def _gather_context(profile_path: Path, mode: str = "files"):
    """Read the profile and the workspace; return (profile_or_none, reason, intake, runs, initiatives, problems, efficiency)."""
    text = _read_text_or_none(profile_path)
    if text is None:
        return None, f"no profile at {profile_path}", [], [], [], [], None
    try:
        profile = route.parse_profile(text)
    except route.ProfileError as exc:
        return None, f"profile unreadable: {exc}", [], [], [], [], None
    workspace = profile.get("workspace_dir", "")
    if not workspace:
        return profile, "workspace_dir not set in profile", [], [], [], [], None
    ws = Path(workspace).expanduser()
    pid_paths = sorted((ws / "runs").glob("*.pid"))
    pids = {p.stem: t for p in pid_paths if (t := _read_text_or_none(p)) is not None}
    alive = {rid: epic.run_live(route.parse_pid(t), ws / "runs" / f"{rid}.pid") for rid, t in pids.items()}
    started = {rid: _mtime_iso(ws / "runs" / f"{rid}.pid") for rid in pids}
    # the initiative id is the DIRECTORY name: work/<initiative>/<phase>/<task>.md;
    # the edge only reads and names the path parts — route.work_item normalises
    items = _stored_work_items(ws, mode)
    return (profile, "",
            _intake_groups(ws, items),
            _with_remote_lanes(ws / "runs", route.run_entries(pids, alive, started, _heartbeats(ws / "runs", pids))),
            route.initiative_summaries(items),
            route.state_problems(items),
            _efficiency(ws / "runs", _now_iso()))


def _work_items(ws: Path) -> list:
    return [
        route.work_item(fields, initiative=task_path.parent.parent.name, phase_dir=task_path.parent.name, stem=task_path.stem)
        for task_path in sorted((ws / "work").glob("*/*/*.md"))
        if task_path.name != "initiative.md"
        for text in [_read_text_or_none(task_path)] if text is not None
        for fields in [route.parse_frontmatter(text)[0]]
    ]


def _stored_work_items(ws: Path, mode: str) -> list:
    """Edge. `_work_items` with each state from the store under mode "store"; the store is not read under "files"."""
    items = _work_items(ws)
    return route.with_store_states(items, run_store.work_items(ws / "runs") if mode == "store" else [], mode)


def _initiative_texts(ws: Path) -> dict:
    return {
        p.name: _read_text_or_none(p / "initiative.md") or ""
        for p in sorted((ws / "work").glob("*"))
        if (p / "initiative.md").is_file()
    }


def _intake_groups(ws: Path, items: list) -> dict:
    intake_root = ws / "intake"
    paths = sorted(intake_root.glob("*.md")) + sorted((intake_root / "done").glob("*.md"))
    files = {str(p.relative_to(intake_root)): t for p in paths if (t := _read_text_or_none(p)) is not None}
    texts = _initiative_texts(ws)
    states = route.initiative_states(sorted(texts), items)
    initiatives = [{"id": iid, "done": states[iid], "text": texts[iid]} for iid in texts]
    return route.intake_groups(route.intake_entries(files), initiatives)


# Commands that run before a store is configured, or configure it. Every other command needs the one store.
_STORE_FREE = frozenset({"setup", "install", "upgrade", "versions", "settings", "dev", "release", "home", "plan"})


STORE_DOUBLE_ENV = "COXSWAIN_STORE_DOUBLE"  # the suites' sqlite stand-in; the harness reads the same name


def store_refusal(head: Sequence[str], storage: object, environ: Mapping[str, str]) -> str | None:
    """Pure. Why the command line `head` may not run against the store URL `storage`; None when it may.
    The one store is Postgres. No URL, or any other kind, is refused for every command that touches the board."""
    words = [w for w in head if not w.startswith("-")]
    if not words or words[0] in _STORE_FREE or tuple(words[:2]) in _STORE_FREE_COMMANDS or environ.get(STORE_DOUBLE_ENV) == "sqlite":
        return None
    if not isinstance(storage, str) or not storage:
        return "no store is configured: the provider profile needs a storage_url naming a postgresql:// store. Run `cox setup store --store shared`."
    try:
        shared = store_dialect.is_postgres(storage)
    except ValueError:
        shared = False
    return None if shared else (
        "the configured store is not Postgres: sqlite and file-only work state are retired. "
        "Run `cox setup store --store shared`, then `cox setup store switch shared` to carry the old rows over."
    )


def _store_refusal(a: argparse.Namespace, head: Sequence[str]) -> str | None:
    """Edge. `store_refusal` for the profile in use. A machine with no routing profile yet is not refused: it has
    nothing configured to be wrong, and `cox setup` is how it gets one."""
    text = _read_text_or_none(_profile_path(a))
    if text is None:
        return None
    try:
        named = route.parse_profile(text).get("provider_profile")
    except route.ProfileError:
        return None  # the command reports the unreadable profile itself
    provider = store_url.read_provider_profile(named) if named else {}
    return store_refusal(head, provider.get("storage_url"), os.environ)


_EXPORT_STAMP = ".export-stamp"
# Commands that read the board from this machine's files. `chair` is not here: its tick exports for itself.
_BOARD_READERS = frozenset({"route", "runs", "lander", "epic", "console", "dash"})
_BOARD_WRITERS = frozenset({("route", "export"), ("route", "import"), ("route", "store-fill")})


def _note_export_stamp(runs_dir: Path, stamp: object) -> None:
    """Edge: remember the store version this machine's files were last written from. A failure only costs a re-export."""
    if stamp is None:
        return
    with contextlib.suppress(OSError):
        (runs_dir / _EXPORT_STAMP).write_text(json.dumps(stamp) + "\n", encoding="utf-8")


def reads_board(head: Sequence[str]) -> bool:
    """Pure: whether the command line `head` runs a command that reads the board files and does not write them itself."""
    words = [w for w in head if not w.startswith("-")]
    return bool(words) and words[0] in _BOARD_READERS and tuple(words[:2]) not in _BOARD_WRITERS


def _refresh_board_files(a: argparse.Namespace) -> None:
    """Edge. With the workspace out of git, bring this machine's work/ and intake/ files up to the store before a
    command reads them: one cheap version probe, and an export only when the store changed since the last one.
    Never raises and never prints on success: a store that cannot be reached leaves the files as they are."""
    try:
        text = _read_text_or_none(_profile_path(a))
        profile = route.parse_profile(text) if text else {}
        if not profile.get("workspace_dir") or workspace_git_on(profile):
            return
        workspace = Path(profile["workspace_dir"]).expanduser()
        runs_dir = workspace / "runs"
        stamp = run_store.queue_stamp(runs_dir)
        if stamp is None:
            return
        last = _read_text_or_none(runs_dir / _EXPORT_STAMP)
        if last is not None and last.strip() == json.dumps(stamp):
            return
        _export_board(runs_dir, workspace, prune_stale=True)
        _note_export_stamp(runs_dir, stamp)
    except Exception as exc:  # a refresh is a convenience; the command still runs on the files it has
        print(f"export: could not refresh this machine's board files from the store: {type(exc).__name__}: {exc}", file=sys.stderr)


def _export_board(runs_dir: Path, workspace: Path, prune_stale: bool = False) -> list[str]:
    """Edge. Write the whole store board under `workspace` and return the ids of the contentless rows skipped.

    The whole board, never one initiative: `export_files` deletes every intake/ and work/ file its rows do not name.
    So it fills the store from the files first: a decompose writes its tickets as files, and an export with no fill
    deleted them, on the chair's tick and on an epic launch alike (2026-10-06). A fill that cannot read the store
    raises before anything is written or deleted. `prune_stale` is for a workspace out of git: see `fill_workspace`."""
    from agent_tools import store_fill, workspace_lock  # imported here: cli loads a command module only when it runs

    # One hold from the fill to the last write: a pull between them would bring a file the fill never read, and the
    # export would delete it as a file with no row. A lock held past its bound raises `WorkspaceBusy`.
    with workspace_lock.held(workspace, "export"):
        # The argument is passed only when set, so a caller's stand-in for the plain fill keeps working.
        store_fill.fill_workspace(workspace, prune_stale=True) if prune_stale else store_fill.fill_workspace(workspace)
        # A file with no row is not necessarily orphaned: it may be a live decompose's, whose rows are not written yet.
        live = chair_decompose_runs.live_decompose_initiatives(runs_dir)
        rows = run_store.read_queue(runs_dir)
        return queue_export.export_files(workspace, rows, hashes=queue_export.store_hashes(runs_dir), protected=live).skipped


def workspace_git_on(profile: Mapping) -> bool:
    """Whether the chair keeps the workspace in git: `chair.workspace_git`, on unless the profile says false.

    Off, the store is the only shared copy of the board. The work/ and intake/ trees are then this machine's own
    files, written from the store so everything that reads them still can: the chair neither pulls the workspace
    nor commits and pushes what it exports, and any machine refreshes its copy with `cox route export`."""
    chair = profile.get("chair")
    return not (isinstance(chair, Mapping) and chair.get("workspace_git") is False)
