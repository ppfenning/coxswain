"""The dashboard feed, assembled: the edges each section reads through, `gather_feed` and the cache."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType

from agent_tools import (
    chair,
    chair_activity_read,
    console_screen,
    courier,
    dash_board,
    dash_feed_cost,
    dash_feed_inputs,
    dash_feed_memo,
    dash_live_agents,
    dash_live_burn,
    dash_live_series,
    dash_live_turns_lane,
    dash_live_turns_session,
    dash_machine_lanes,
    jsonl_log,
    queue_rows,
    run_store,
)
from agent_tools.chair_read_exits import exit_rows
from agent_tools.chair_read_housekeeping import latest_housekeeping, read_last_housekeeping
from agent_tools.chair_read_record import ACTION_LOG
from agent_tools.chair_read_review_prs import read_review_prs
from agent_tools.chair_read_schema_deaths import log_tail
from agent_tools.chair_schedule import SCHEDULE_FILE
from agent_tools.chair_types import EASTERN, ReviewPr
from agent_tools.dash_chair_action import read_current_action
from agent_tools.dash_chair_attention import chair_attention
from agent_tools.dash_chair_beat import beat_age_s, read_status_record, status_from_store
from agent_tools.dash_chair_counters import _age_s as _stamp_age_s
from agent_tools.dash_chair_counters import chair_counters
from agent_tools.dash_chair_store import _iso, _liveness, _tick_fields, chair_from_store, read_chair_rows
from agent_tools.dash_chair_today import local_midnight
from agent_tools.dash_cost_series import run_cost_series
from agent_tools.dash_feed_sections import (
    _LIVE_WINDOW,
    _LOG_TAIL_BYTES,
    _PROJECTS_DIR,
    _ZERO_HISTORY_TODAY,
    _activity,
    _activity_spend_key,
    _age_s,
    _as_results,
    _attempts_by_task,
    _budget_edge,
    _building_tasks,
    _call_points,
    _chair_v1,
    _courier_blob,
    _current_release_edge,
    _epics_v1,
    _finished_records,
    _hard_stop_fraction,
    _inbox,
    _intake_v1,
    _latest_run,
    _live_lanes,
    _local_identity,
    _local_login,
    _local_versions,
    _machine_v1,
    _open_decisions,
    _orchestration_edge,
    _parse_now,
    _project,
    _projects,
    _queue,
    _queue_rows_edge,
    _queue_states_edge,
    _reaches_a_person,
    _run_calls,
    _run_slug,
    _run_v1,
    _short_id,
    _spend,
    _spend_points,
    _spend_v1,
    _started_at,
    _task_slug,
    _tasks_edge,
    _tick_age_s,
    _today_v1,
    _utc,
    _window_starts,
    _workspace,
    _zero_live,
    snapshot,
)
from agent_tools.dash_finished_runs import finished_runs
from agent_tools.dash_history import _by_run, _in_task_order, _landed_pr, _pr_number, _record, build_history
from agent_tools.dash_history import history_today as _history_today
from agent_tools.dash_history_store import RECENT_ENDED, read_history_rows, read_open_task_records
from agent_tools.dash_live_turns_lane import advance_lane_cursor
from agent_tools.dash_live_turns_session import read_session_turns_with_cursor
from agent_tools.dash_run_end import End, run_end
from agent_tools.dash_spend_series import spend_series as _spend_series
from agent_tools.dash_transcript_cursor import CursorState
from agent_tools.draft_list import read_drafts


def _lanes_by_host(
    lanes: Sequence[console_screen.LaneRow], states: Mapping[tuple[str, str, str], str],
    calls_by_run: Mapping[str, Sequence[Mapping]], now: datetime, local_name: str,
) -> dict[str, list[dict]]:
    """The live lanes as `machines[].lanes` entries by host; a lane with no host of its own is on this machine."""
    aware = now if now.tzinfo else now.replace(tzinfo=UTC)
    return dash_machine_lanes.lane_entries(
        lanes, lambda run: None, {lane.run: _run_slug(lane.run) for lane in lanes}, _building_tasks(states),
        _started_at(calls_by_run), aware, local_host=local_name,
    )


def _board_runs(
    attempts_by_task: Mapping[str, Sequence[Mapping]], lanes: Sequence[console_screen.LaneRow], local_name: str,
) -> dict[str, tuple[str, str]]:
    """Each task's `(run, host)` when its latest attempt is in a live lane; a task of a finished run has none."""
    hosts = {lane.run: lane.host or local_name for lane in lanes}
    latest = {task: _latest_run(rows) for task, rows in attempts_by_task.items()}
    return {task: (run, hosts[run]) for task, run in latest.items() if run in hosts}


def _board_attempts(
    runs_dir: Path, rows: Sequence[Mapping], calls_by_run: Mapping[str, Sequence[Mapping]],
) -> dict[str, list[Mapping]]:
    """Edge: the attempts of every stored run of the queue's initiatives, by task id, as `_tasks_edge` selects them."""
    slugs = {str(row["initiative"]) for row in rows if row.get("kind") == "task" and row.get("initiative")}
    if not slugs:
        return {}
    stored = {str(row["run_id"]) for row in exit_rows(runs_dir) if row.get("run_id")}
    attempts = run_store.attempt_causes_for(
        runs_dir, sorted(run for run in {*stored, *calls_by_run} if _run_slug(run) in slugs),
    )
    return {task: grouped for slug in slugs for task, grouped in _attempts_by_task(attempts, slug).items()}


def _board_edge(
    runs_dir: Path, rows: Sequence[Mapping], states: Mapping[tuple[str, str, str], str],
    calls_by_run: Mapping[str, Sequence[Mapping]], lanes: Sequence[console_screen.LaneRow],
    short_ids: Mapping[str, str], local_name: str, now: datetime,
) -> tuple[dict, dict[str, list[dict]]]:
    """Edge: `(board, lanes_by_host)` for the feed. Only store and file errors are caught, as in `_tasks_edge`;
    the attempts cannot be read then, and the board and the lanes are both empty. A code fault raises."""
    try:
        attempts = _board_attempts(runs_dir, rows, calls_by_run)
    except (OSError, *run_store._db_errors()):  # a store failure must not take the feed down; a code fault must
        return {"epics": []}, {}
    epic_of = {
        str(row["initiative"]): queue_rows.epic_of_row(row)
        for row in rows
        if row.get("kind") == "initiative" and row.get("initiative")
    }
    tasks = [row for row in rows if row.get("kind") == "task" and row.get("task_id")]
    board = dash_board.build_board(
        tasks, states, _board_runs(attempts, lanes, local_name), epic_of, short_ids,
        dash_board.task_costs(attempts, calls_by_run),
    )
    return board, _lanes_by_host(lanes, states, calls_by_run, now, local_name)


def _burn_meters(spend: Mapping, split: Mapping, ceiling: float) -> dict[str, dict]:
    """The two burn meters: used fraction and reset from `spend`, window spend from the split's `cox_seats_usd`."""
    return {
        name: {
            "used_fraction": float(spend.get(f"{name}_fraction") or 0.0),
            "window_spend_usd": float((split.get(name) or {}).get("cox_seats_usd") or 0.0),
            "ceiling_fraction": ceiling,
            "resets_at": str(spend.get(f"{name}_resets_at") or ""),
        }
        for name in dash_live_burn.METER_NAMES
    }


def _run_hosts(rows: Mapping) -> dict[str, str]:
    """Each stored run's host, for the runs that record one."""
    return {str(r["run_id"]): str(r["host"]) for r in rows.get("runs", ()) if r.get("run_id") and r.get("host")}


def _ended_events(rows: Mapping) -> list[dict]:
    """The `ts` and `outcome` of every landed or quarantined run in the wide store rows. Not the history cut: that one
    drops what ended before local midnight, and the per-hour series reach back 24 hours."""
    runs = rows.get("runs", ())
    history = build_history(runs, rows.get("task_records", ()), rows.get("node_calls", ()), limit=len(runs))
    return [{"ts": str(h["ended_at"]), "outcome": h["outcome"]} for h in history if h["outcome"] in ("landed", "quarantined")]


def _lane_targets(
    lanes: Sequence[console_screen.LaneRow], states: Mapping[tuple[str, str, str], str],
    records: Sequence[Mapping], hosts: Mapping[str, str], local_name: str, at: datetime,
) -> list[dict]:
    """What to read lane turns for: each live lane, and each run that ended in the last 60 minutes. A finished run is
    on the host its stored row names, or on this machine when the store records none."""
    building = _building_tasks(states)
    live = [
        {
            "run": lane.run, "host": lane.host or local_name, "initiative": _run_slug(lane.run),
            "task": next(iter(building.get((_run_slug(lane.run), lane.phase), [])), ""), "node": lane.node,
        }
        for lane in lanes
    ]
    recent = [
        {
            "run": str(r["run"]), "host": hosts.get(str(r["run"])) or local_name, "initiative": _run_slug(str(r["run"])),
            "task": "",
            "node": str(r.get("node") or ""),
        }
        for r in records
        if (ended := _utc(r.get("ended_at"))) is not None and timedelta(0) <= at - ended <= _LIVE_WINDOW
    ]
    return [*live, *recent]


def _assemble_live(
    turns: Sequence[Mapping], meters: Mapping, events: Sequence[Mapping], windows: Mapping[str, int], at: datetime,
) -> dict:
    """The live block from the turns, the burn meters, the `{ts, outcome}` end events and the context windows."""
    landed = [event for event in events if event["outcome"] == "landed"]
    quarantined = [event for event in events if event["outcome"] == "quarantined"]
    return {
        "throughput": dash_live_series.throughput(turns, at),
        "burn": dash_live_burn.burn(turns, meters, at),
        "agents": dash_live_agents.agents(turns, at, windows),
        "calls_per_min": dash_live_series.calls_per_min(turns, at),
        "cache_hit_pct": dash_live_series.cache_hit_pct(turns, at),
        "lands_per_hour": dash_live_series.per_hour(landed, at, 24),
        "quarantines_per_hour": dash_live_series.per_hour(quarantined, at, 24),
    }


@dataclass(frozen=True)
class LiveCursors:
    """The read positions the live block carries between refreshes: the session transcripts' cursor, and one lane
    cursor per (run, node) target."""

    session: CursorState | None = None
    lanes: Mapping[tuple[str, str], CursorState] = MappingProxyType({})


def _live_edge(
    runs_dir: Path, targets: Sequence[Mapping], meters: Mapping, events: Sequence[Mapping], profile: dict,
    local_name: str, chair_session: str, at: datetime,
    cursors: LiveCursors | None = None, on_cursors: Callable[[LiveCursors], None] | None = None,
) -> dict:
    """Edge: the live block from lane traces and session transcripts. `events` are the landed and quarantined end
    events of the wide store rows. Only store and file errors are caught, as in `_tasks_edge`; the zero block then
    stands. The agents' context windows are the price catalog's own, else the module's fallback.
    With `cursors`, only bytes past each cursor are parsed. The advanced cursors go to `on_cursors`, and only when
    the block was read; on the zero block the caller keeps the cursors it had. Without `cursors` every file is
    read whole."""
    moment = at if at.tzinfo else at.replace(tzinfo=UTC)
    try:
        prices = chair_activity_read.prices_for_profile(profile)
        windows = dash_live_agents.default_context_windows(chair_activity_read.catalog_for_profile(profile))
        lane_turns: list[dict] = []
        lane_states: dict[tuple[str, str], CursorState] = {}
        for t in targets:
            key = (t["run"], t["node"])
            if cursors is not None:
                lane_states[key] = advance_lane_cursor(runs_dir, t["run"], t["node"], cursors.lanes.get(key))
            lane_turns += dash_live_turns_lane.read_lane_turns(
                runs_dir, t["run"], t["host"], t["initiative"], t["task"], t["node"], prices,
                **({} if cursors is None else {"cursor": lane_states[key]}),
            )
        projects_dir, since = Path(_PROJECTS_DIR).expanduser(), moment - _LIVE_WINDOW
        session_state = None
        if cursors is None:
            session = dash_live_turns_session.read_session_turns(
                projects_dir, local_name, chair_session, since, prices=prices,
            )
        else:
            session, session_state = read_session_turns_with_cursor(
                projects_dir, local_name, chair_session, since, cursors.session, prices=prices,
            )
    except (OSError, *run_store._db_errors()):  # a store failure must not take the feed down; a code fault must
        return _zero_live(moment)
    if cursors is not None and on_cursors is not None:
        on_cursors(LiveCursors(session_state, MappingProxyType(lane_states)))
    turns = sorted([*lane_turns, *session], key=lambda turn: turn["ts"])
    return _assemble_live(turns, meters, events, windows, moment)


def _runs_v1(
    lanes: Sequence[console_screen.LaneRow], local_name: str, records: Sequence[Mapping], now: datetime,
    calls_by_run: Mapping[str, Sequence[Mapping]] | None = None, short_ids: Mapping[str, str] | None = None,
    projects: Mapping[str, str | None] | None = None, ends: Mapping[str, End] | None = None,
    tasks_by_slug: Mapping[str, tuple[list[dict], dict]] | None = None,
) -> list[dict]:
    """The live lanes as running rows, then the newest finished runs; a finished record of a live lane is dropped.
    Each row gains `cost_series` from its run's entry in `calls_by_run`; a run with no entry gets `[]`.
    Each row gains `short_id` from `short_ids` and `project` from `projects`, by its run key's initiative slug.
    Each row gains `end`: `running` for a lane, else its entry in `ends`, else `idle`. `status` is unchanged.
    When `tasks_by_slug` is given, each row gains `tasks`, `landed` and `quarantined` from its initiative's entry;
    an initiative with no entry gets `[]`, 0 and 0. Without it the rows carry none of the three keys."""
    aware = now if now.tzinfo else now.replace(tzinfo=UTC)
    live = {lane.run for lane in lanes}
    rows = [*(_run_v1(lane, local_name) for lane in lanes), *finished_runs(records, live, aware)]
    ended = ends or {}
    by_run = calls_by_run or {}
    ids = short_ids or {}
    named = projects or {}
    empty: tuple[list[dict], dict] = ([], {"landed": 0, "quarantined": 0, "merged": 0})

    def task_keys(run: str) -> dict:
        entries, tally = (empty if tasks_by_slug is None else tasks_by_slug.get(_run_slug(run), empty))
        return {} if tasks_by_slug is None else {"tasks": entries, **tally}

    return [
        {
            **row,
            "cost_series": run_cost_series(_call_points(by_run.get(row["run"], ()))),
            "short_id": _short_id(_run_slug(row["run"]), ids),
            "project": _project(_run_slug(row["run"]), named),
            "end": "running" if row["run"] in live else ended.get(row["run"], "idle"),
            **task_keys(row["run"]),
        }
        for row in rows
    ]


def _queue_v1(row: dict, ids: Mapping[str, str], projects: Mapping[str, str | None]) -> dict:
    return {
        **row, "priority": int(row.get("priority") or 0), "current_phase": str(row.get("current_phase") or ""),
        "short_id": _short_id(row.get("initiative"), ids),
        "project": _project(row.get("initiative"), projects),
    }


_REF = re.compile(r"coxswain://([^/]+)/(.*)")


def _inbox_v1(entry: dict, ids: Mapping[str, str]) -> dict:
    """A courier entry as the schema-1 inbox row: the reference's kind and id, and the note as the reason.
    A `task` row takes `short_id` by its target's longest slug prefix; courier entries carry no initiative."""
    match = _REF.fullmatch(str(entry.get("ref") or ""))
    kind, target = match.groups() if match else ("", str(entry.get("ref") or ""))
    slug = _task_slug(target, ids) if kind == "task" else None
    return {"kind": kind, "target": target, "reason": str(entry.get("note") or ""), "short_id": _short_id(slug, ids)}


def _review_inbox_v1(review_prs: Sequence[ReviewPr], ids: Mapping[str, str]) -> list[dict]:
    """One `review_pr` inbox row per awaiting review PR, whatever its state; `target` is the task id."""
    return [
        {
            "kind": "review_pr", "initiative": pr["initiative"], "target": pr["task_id"], "url": pr["url"],
            "reason": "awaiting review", "short_id": _short_id(pr["initiative"], ids),
        }
        for pr in review_prs
    ]


def _lease(runs_dir: Path) -> dict:
    try:
        raw = json.loads((runs_dir / "chair.lease.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _schedule_edge(runs_dir: Path) -> dict | None:
    """The `schedule` object of runs/SCHEDULE_FILE, unchanged; None when the document is absent, unparsable or lacks one."""
    try:
        raw = json.loads((runs_dir / SCHEDULE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    schedule = raw.get("schedule") if isinstance(raw, dict) else None
    return schedule if isinstance(schedule, dict) else None


def _chair_record(runs_dir: Path) -> dict:
    """runs/chair.json via `chair.read`; {} when absent or unreadable, as `runs_top_screen.chair_now` does."""
    try:
        record = chair.read(runs_dir)
    except (OSError, json.JSONDecodeError):
        record = None
    return record if isinstance(record, dict) else {}


def _action_rows(runs_dir: Path) -> list[dict]:
    """The chair action log's lines as dicts; [] when it is missing, blank lines and damaged ones skipped."""
    return list(jsonl_log.read_log(runs_dir / ACTION_LOG).rows)


def _health(work_dir: Path, runs_dir: Path) -> dict:
    """Edge: the health block, one row per log with a non-zero skipped-line count. courier.jsonl lives in `work_dir`."""
    logs = (
        ("courier.jsonl", Path(work_dir) / "courier.jsonl"),
        ("land.jsonl", Path(runs_dir) / "land.jsonl"),
        (ACTION_LOG, Path(runs_dir) / ACTION_LOG),
    )
    counted = [{"log": name, "skipped": jsonl_log.read_log(path).skipped} for name, path in logs]
    damaged = [row for row in counted if row["skipped"]]
    return {"skipped_total": sum(row["skipped"] for row in damaged), "logs": damaged}


def open_needs_chair_ids(entries: list[dict]) -> list[str]:
    """The ids of unacknowledged courier entries that reach a person and are `needs_chair` refs, before the inbox cap."""
    refs = (_REF.fullmatch(str(e.get("ref") or "")) for e in entries if _reaches_a_person(str(e.get("to") or "")))
    return [m.group(2) for m in refs if m and m.group(1) == "needs_chair"]


def _needs_chair_open(entries: list[dict]) -> int:
    return len(open_needs_chair_ids(entries))


def _chair_edge(runs_dir: Path) -> dict:
    """Edge: the new chair reads as `_chair_v1` keyword arguments.

    The status line is the newest `status` row in the store; chair-loop.log's last line only when the store has none.
    """
    stored = status_from_store(run_store.latest_chair_status(runs_dir))
    action_rows = _action_rows(runs_dir)
    return {
        "status": stored if stored != (None, None) else read_status_record(runs_dir / "chair-loop.log"),
        "action": read_current_action(runs_dir),
        "action_rows": action_rows,
        "last_housekeeping_at": latest_housekeeping(action_rows),
    }


def _stamp(tick_at: str | None) -> str:
    """The status line's stamp (`MM-DD HH:MM TZ`, Eastern) for an ISO `tick_at`; `tick_at` as given when it is not aware ISO."""
    try:
        then = datetime.fromisoformat(tick_at) if tick_at else None
    except ValueError:
        return tick_at or ""
    return f"{then.astimezone(EASTERN):%m-%d %H:%M %Z}" if then is not None and then.tzinfo else tick_at or ""


def _chair_from_store_v1(
    lease: dict, rows: list[dict], record: dict, at: datetime, needs_chair_open: int,
    last_housekeeping_at: str | None, inbox_entries: list[dict], drafts: list,
) -> dict:
    """The schema-1 chair from the store's lease row and `chair_actions` rows. Pure: `at` is the one clock.

    `chair_from_store` has no session, counter or attention keys, and its `last_tick_at` is ISO where the feed prints
    the status line's Eastern stamp, so those come from `record`, `rows` and the inbox here. The store's values win.
    """
    offset = at.astimezone(EASTERN).utcoffset()
    stored = chair_from_store(lease, rows, needs_chair_open, at, offset)
    extras = {
        **chair_counters(_as_results(rows, at, offset), rows, last_housekeeping_at, at, offset),
        **chair_attention(inbox_entries, drafts),
    }
    return {
        "session": str(record.get("claude_session") or "")[:8],
        **{key: int(value or 0) for key, value in extras.items()},
        **stored,
        "last_tick_at": _stamp(stored["last_tick_at"]),
        "last_status": stored["last_status"] or "",
    }


def _iso_ts(row: dict) -> dict:
    """`row` with a Postgres `datetime` ts as ISO text, the form `chair_today` reads."""
    ts = row.get("ts")
    return {**row, "ts": ts.isoformat()} if isinstance(ts, datetime) else row


def _chair_parts(
    runs_dir: Path, row: dict, entries: list[dict], drafts: list, at: datetime,
) -> tuple[dict, tuple]:
    """Edge: the chair from the store, and from the local record files only when the store fetch returns None, with
    the raw stamps its age fields count from: (source, heartbeat_at, tick_at, last_housekeeping_at, holder, expires_at).
    Each stamp is the one the chosen path reads: the lease's on the store path, chair.json's and the status line's
    on the record path, so `_chair_ages` can redo the ages at a later `at` and agree with this function."""
    offset = at.astimezone(EASTERN).utcoffset()
    needs_chair_open = _needs_chair_open(entries)
    # Other seats drop out as in `_inbox`; `chair_attention` drops `chair` itself.
    people = [e for e in entries if _reaches_a_person(str(e.get("to") or ""))]
    fetched = read_chair_rows(runs_dir, at, offset)
    record = _chair_record(runs_dir)
    if fetched is None:
        edge = _chair_edge(runs_dir)
        block = _chair_v1(
            row, _lease(runs_dir), record, at,
            needs_chair_open=needs_chair_open, inbox_entries=people, drafts=drafts, **edge,
        )
        return block, ("record", record.get("heartbeat_at"), edge["status"][0], edge["last_housekeeping_at"], "", None)
    lease, raw_rows = fetched
    rows = [_iso_ts(r) for r in raw_rows]
    # The fetch reaches back about a day, so a housekeeping row older than that is read whole from the store.
    last_housekeeping_at = latest_housekeeping(rows) or read_last_housekeeping(runs_dir)
    block = _chair_from_store_v1(lease, rows, record, at, needs_chair_open, last_housekeeping_at, people, drafts)
    stamps = (
        "store", _iso(lease.get("heartbeat_at")), _tick_fields(lease.get("status"), at)["last_tick_at"],
        last_housekeeping_at, str(lease.get("holder") or ""), lease.get("expires_at"),
    )
    return block, stamps


def _chair_section(runs_dir: Path, row: dict, entries: list[dict], drafts: list, at: datetime) -> dict:
    """Edge: the chair block of `_chair_parts`."""
    return _chair_parts(runs_dir, row, entries, drafts, at)[0]


def _chair_ages(stamps: tuple, at: datetime) -> dict:
    """The chair's age-derived fields at `at`, from the stamps `_chair_parts` returned, by the formulas of the path
    that produced them. `liveness` moves with `at` on the store path only; on the record path it is the console row's."""
    source, heartbeat_at, tick_at, housekeeping_at, holder, expires_at = stamps
    age = beat_age_s(heartbeat_at, at) if isinstance(heartbeat_at, str) else None
    ages = {
        "beat_age_s": max(0, age) if age is not None else 0,
        "housekeeping_age_s": _stamp_age_s(housekeeping_at, at),
    }
    if source == "store":
        tick = beat_age_s(tick_at, at) if isinstance(tick_at, str) else None
        return {**ages, "tick_age_s": tick, "liveness": _liveness(holder, expires_at, at)}
    return {**ages, "tick_age_s": _tick_age_s(tick_at, at)}


def _history_tasks(
    run_tasks: Sequence[Mapping], initiative: str, states: Mapping[tuple[str, str, str], str],
) -> list[dict]:
    """One `{id, state, pr}` per task record the run built, in phase then task order. `state` is the queue's
    current state for the task (`_queue_states`), `pr` the PR number in the record's `landed` dict, else None.
    A task the queue has no state for is left out rather than guessed."""
    return [
        {"id": row.get("task_id"), "state": state, "pr": _pr_number(_landed_pr(_record(row.get("record_json")).get("landed")))}
        for row in _in_task_order(run_tasks)
        if (state := states.get((initiative, row.get("phase_id"), row.get("task_id")))) is not None
    ]


def _inherit_prs(tasks_by_run: Sequence[list[dict]], initiatives: Sequence[str]) -> list[list[dict]]:
    """`tasks_by_run` (newest run first) with each task's missing `pr` taken from the nearest later run of the same
    initiative that has one for the same task id. A run's own `pr` always wins; none anywhere stays None."""
    def later(index: int, task_id: object) -> int | None:
        return next(
            (t["pr"] for i in range(index - 1, -1, -1) if initiatives[i] == initiatives[index]
             for t in tasks_by_run[i] if t["id"] == task_id and t["pr"] is not None),
            None,
        )

    return [[t if t["pr"] is not None else {**t, "pr": later(i, t["id"])} for t in tasks] for i, tasks in enumerate(tasks_by_run)]


def _history_v1(
    rows: Mapping, at: datetime, ids: Mapping[str, str], projects: Mapping[str, str | None],
    states: Mapping[tuple[str, str, str], str] | None = None,
) -> tuple[list[dict], dict]:
    """`history` and `history_today` from store rows, counting a day in the chair's zone.
    Each history row gains `short_id` and `project` by its `initiative`, and `tasks`: its tasks' current state
    and landed PR (`_history_tasks`). `outcome` still records how the run ended and is never rewritten."""
    offset = at.astimezone(EASTERN).utcoffset() or timedelta(0)
    args = (rows["runs"], rows["task_records"], rows["node_calls"])
    by_run = _by_run(rows["task_records"])
    built = build_history(*args)
    tasks = _inherit_prs(
        [_history_tasks(by_run.get(str(row["run"]), []), row["initiative"], states or {}) for row in built],
        [row["initiative"] for row in built],
    )
    history = [
        {
            **row,
            "short_id": _short_id(row.get("initiative"), ids),
            "project": _project(row.get("initiative"), projects),
            "tasks": row_tasks,
        }
        for row, row_tasks in zip(built, tasks, strict=True)
    ]
    return history, _history_today(*args, at, timezone(offset))


def _history_rows(rows: Mapping, at: datetime) -> Mapping:
    """`rows` cut back to what the history reads: runs ended since local midnight or among the newest `RECENT_ENDED`
    (the reader returns runs newest first), with those runs' records and calls."""
    if not rows:
        return rows
    offset = at.astimezone(EASTERN).utcoffset() or timedelta(0)
    midnight = local_midnight(at, offset)
    runs = [
        run
        for index, run in enumerate(rows.get("runs", ()))
        if index < RECENT_ENDED or (_utc(run.get("ended_at")) or midnight) >= midnight
    ]
    ids = {run.get("run_id") for run in runs}
    return {**rows, "runs": runs, **{t: [r for r in rows.get(t, ()) if r.get("run_id") in ids] for t in ("task_records", "node_calls")}}


def _store_rows(runs_dir: Path, at: datetime, spend: Mapping) -> Mapping:
    """Edge: the ended runs since the earliest of local midnight and the spend windows' starts, or the newest ones,
    with their task records and calls. Read once, wide enough for the weekly spend split; `_history_rows` narrows it.
    `open_task_records` adds the task records of runs still in flight written since then, which the history never
    reads and the spend windows count as completions once merged. An unreadable store gives no rows."""
    offset = at.astimezone(EASTERN).utcoffset() or timedelta(0)
    starts = [local_midnight(at, offset), *_window_starts(spend, at)]
    since = min(start.astimezone(UTC) for start in starts).isoformat()
    try:
        return {**read_history_rows(runs_dir, since), "open_task_records": read_open_task_records(runs_dir, since)}
    except Exception:  # the history and ends are garnish: a store failure must not take the feed down
        return {}


def _history_edge(
    rows: Mapping, at: datetime, ids: Mapping[str, str], projects: Mapping[str, str | None],
    states: Mapping[tuple[str, str, str], str] | None = None,
) -> tuple[list[dict], dict]:
    """The feed's history from the store rows. Rows that cannot be read give no history and zero counts."""
    try:
        return _history_v1(rows, at, ids, projects, states)
    except Exception:  # the history is garnish: a store failure must not take the feed down
        return [], dict(_ZERO_HISTORY_TODAY)


def _ends_v1(rows: Mapping, log_tails: Mapping[str, str]) -> dict[str, End]:
    """Each ended run's id mapped to `run_end` over its task records and its log tail."""
    tasks: dict[str, list[Mapping]] = {}
    for record in rows.get("task_records", ()):
        tasks.setdefault(str(record.get("run_id")), []).append(record)
    return {
        str(run["run_id"]): run_end(run, tasks.get(str(run["run_id"]), ()), log_tails.get(str(run["run_id"]), ""))
        for run in rows.get("runs", ())
        if run.get("run_id")
    }


def _ends_edge(runs_dir: Path, rows: Mapping) -> dict[str, End]:
    """Edge: the ends of the store's ended runs, reading each run's log tail. A failure gives no ends."""
    try:
        tails = {
            str(run["run_id"]): log_tail(runs_dir, str(run["run_id"]), _LOG_TAIL_BYTES) for run in rows.get("runs", ())
        }
        return _ends_v1(rows, tails)
    except Exception:  # the ends are garnish: a store failure must not take the feed down
        return {}


def _console_chair(runs_dir: Path) -> list[dict]:
    """Edge: the console's chair rows as `console_screen.gather` reads them: `runs_top_screen.chair_now`, or none."""
    chair_state = console_screen.runs_top_screen.chair_now(runs_dir)
    return [chair_state] if chair_state is not None else []


def _console_now(
    held: Mapping, spend: dict, local_name: str, local_versions: dict | None, chair_rows: list[dict],
) -> dict:
    """A held console gather as this refresh sees it: `spend` read this refresh, the chair rows read this refresh, and
    the local host row's login check stamped at this refresh, all of which the held gather carries from the refresh
    that computed it. Wrong belief to avoid: "the chair row moves with the store". It reads chair.json, its pid and the
    clock, so it is read afresh on every reuse and the chair block never lags the console hold. A gather run this
    refresh already has all three, so this is applied to a reused one only."""
    if local_versions is None:
        return {**held, "spend": spend, "chair": chair_rows}
    hosts = [
        {**row, "versions_json": local_versions}
        if row.get("name") == local_name and isinstance(row.get("versions_json"), Mapping)
        and set(row["versions_json"]) == set(local_versions)
        else row
        for row in held["hosts"]
    ]
    return {**held, "spend": spend, "chair": chair_rows, "hosts": hosts}


def gather_feed(runs_dir: Path, work_dir: Path, now: str, profile: dict | None = None) -> dict:
    """Edge: the live schema-1 snapshot from the fleet's existing readers, every section mapped to the shape
    tests/fixtures/dash_feed_v1.json fixes (the contract coxtop parses), with no nulls and nothing json can't write,
    except a machine's `login_ok`, which is null while its login is unchecked, a row's `short_id`, which is
    null when its initiative has none, a row's `project`, which is null when its initiative has no known
    repository, and `spend.today`, which is null when the run store is unavailable (its `cost_per_completion` and the
    deprecated alias `per_landed` are null when nothing completed, and `completions` counts tasks done or merged today,
    and its `first_try_rate` when nothing was measured). `spend.today` is a new key
    under schema 1, as `weekly_pace` was: older towpath builds ignore a key they do not know. So is `history[].tasks`,
    one `{id, state, pr}` per task the run built: its current queue state and its landed PR number, or null. So are
    `runs[].tasks`, `runs[].landed` and `runs[].quarantined`: every task of the run's initiative as `task_entries`
    shapes it, and the `counts` over them. They are `[]`, 0 and 0 for an initiative with no task rows, and absent only
    when the queue is empty or unreadable, or a store error hit while they were read: absent means no task data was
    readable, never "no tasks". A task's `waiting_on` is one task id, its first need not yet settled. `profile` is the parsed profile
    the capacity chain reads the team cartridge from. `budget` is the monthly block, with no nulls: period
    "none" and a zero limit and fraction when the profile sets no limit. `live` is a new key under schema 1: token
    series, burn and agents from the lane traces and session transcripts, the zero block when they cannot be read.
    So is `operator_sessions`: every operator session of the workspace's project folder, plus the lease holder's, as
    rows per window with their agents, which the `spend_split` sums; `spend_sources` gains `sessions_found` and
    `sessions_priced`. The key is absent when the activity section did not gather sessions. `intake` is the queued
    intake list, `[]` when there is none, deferred against the chair's release version and built from the same queue
    rows (tests/fixtures/dash_feed_intake_v1.json fixes its records)."""
    return _build(
        Path(runs_dir), Path(work_dir), now, profile, lambda name, compute, anchor=None, extra=(), fresh=None: compute(),
    )


def _build(
    runs_dir: Path, work_dir: Path, now: str, profile: dict | None, hold,
    cursors: LiveCursors | None = None, on_cursors: Callable[[LiveCursors], None] | None = None,
    reader_cache: chair_activity_read.Cache | None = None,
    on_reader_cache: Callable[[chair_activity_read.Cache], None] | None = None,
) -> dict:
    """Edge: the body of `gather_feed`. `hold(name, compute, anchor, extra, fresh)` gives one section's value:
    `compute()` uncached, a memoised value in `gather_feed_cached`. `anchor` is read after a compute only and names the
    stamps the section's ages count from; `extra` is the section's time-dependent inputs already read, for its key;
    `fresh`, when given, is applied to a reused value only, to put back what this refresh read beside the hold.
    `cursors` and `on_cursors` make the live block read incrementally; see `_live_edge`. `reader_cache` and
    `on_reader_cache` do the same for the activity block's operator transcripts; see `_activity`."""
    at = _parse_now(now)
    local_name, local_capacity = _local_identity(runs_dir, profile or {})
    login_ok = _local_login(local_name)
    local_versions = _local_versions(login_ok, now)
    spend = _spend(runs_dir, now)
    sections = hold(
        "console",
        lambda: console_screen.gather(
            runs_dir, work_dir, now, local_name, local_capacity, spend, local_versions=local_versions,
        ),
        extra=(local_name, local_capacity, login_ok),
        fresh=lambda held: _console_now(held, spend, local_name, local_versions, _console_chair(runs_dir)),
    )
    chair_row = sections["chair"][0] if sections["chair"] else {}
    live_phases = _live_lanes(sections["lanes"])
    blob = _courier_blob(work_dir)
    entries = courier.inbox(blob)
    # A state callable that answers nothing: the feed lists each awaiting PR with its url and never asks the forge.
    review_prs = hold("review_prs", lambda: read_review_prs(runs_dir, lambda url: {}))
    lane_ids = {lane.run for lane in sections["lanes"]}
    records = _finished_records(runs_dir, lane_ids)
    calls_by_run = {
        **{run: _run_calls(runs_dir, run) for run in lane_ids},
        **{record["run"]: record["calls"] for record in records},
    }
    short_ids = run_store.initiative_short_ids(runs_dir)
    projects = _projects(run_store.read_queue(runs_dir, kind="initiative"))
    lanes = tuple(sections["lanes"])
    # `today` is added after `_spend_v1`, which would blank its null into a string. Only `today` is held: the rest of
    # the block is `sections["spend"]`, read afresh above, and moves with `now` in ways no key could floor.
    today_spend = hold("spend", lambda: _today_v1(runs_dir, now))
    spend_block = {**_spend_v1(sections["spend"]), "today": today_spend}
    wide_rows = _store_rows(runs_dir, at, spend_block)
    store_rows = _history_rows(wide_rows, at)
    task_rows = hold("queue_rows", lambda: _queue_rows_edge(runs_dir))
    states = hold(
        "queue_states",
        lambda: _queue_states_edge(runs_dir, live_phases, task_rows),
        extra=tuple(sorted(live_phases.items())),
    )

    def queue_block() -> tuple[list[dict], int]:
        queue, total = _queue(runs_dir, live_phases)
        return [_queue_v1(row, short_ids, projects) for row in queue], total

    def inbox_block() -> tuple[list[dict], int]:
        inbox, total = _inbox(entries)
        rows = [*(_inbox_v1(entry, short_ids) for entry in inbox), *_review_inbox_v1(review_prs, short_ids)]
        return rows, total + len(review_prs)

    queue_rows, queue_total = hold("queue", queue_block, extra=lanes)
    inbox_rows, inbox_total = hold("inbox", inbox_block)
    history, today = hold("history", lambda: _history_edge(store_rows, at, short_ids, projects, states), extra=lanes)
    board, lanes_by_host = hold(
        "board",
        lambda: _board_edge(runs_dir, task_rows, states, calls_by_run, sections["lanes"], short_ids, local_name, at),
        lambda: _started_at(calls_by_run),
        lanes,
    )
    chair_block, _ = hold(
        "chair",
        lambda: _chair_parts(runs_dir, chair_row, entries, read_drafts(work_dir, now), at),
        extra=repr(chair_row),
    )
    # The key adds what the block reads beside the store, the run records and the transcripts: the chair session, the
    # profile (prices) and the meter fields of the spend block. Wrong belief to avoid: "activity keys on the whole
    # spend block". Its ages move on every refresh, and the block reads only the windows' resets, sources and fractions.
    activity = hold(
        "activity",
        lambda: _activity(
            runs_dir, profile or {}, chair_block["session"], _workspace(work_dir), spend_block, at, wide_rows,
            reader_cache, on_reader_cache,
        ),
        extra=(chair_block["session"], repr(profile or {}), _activity_spend_key(spend_block)),
    )
    targets = _lane_targets(sections["lanes"], states, records, _run_hosts(wide_rows), local_name, at)
    meters = _burn_meters(spend_block, activity["spend_split"] or {}, _hard_stop_fraction(runs_dir))
    ended = _ended_events(wide_rows)
    # The key adds what the block reads beside the transcripts and lane traces: its targets, meters, end events,
    # chair session and profile. Wrong belief to avoid: "live moves only with new bytes". It also moves with `at`,
    # which the minute floor in `_SECTION_STEP_S` bounds: a reused block is as of the minute's first refresh.
    live = hold(
        "live",
        lambda: _live_edge(
            runs_dir, targets, meters, ended, profile or {}, local_name, chair_block["session"], at, cursors, on_cursors,
        ),
        extra=(targets, meters, ended, chair_block["session"], profile or {}),
    )
    machines = hold(
        "machines",
        lambda: [_machine_v1(row, at, lanes_by_host.get(str(row.get("name") or ""), [])) for row in sections["hosts"]],
        lambda: ([row.get("beat_at") for row in sections["hosts"]], _started_at(calls_by_run)),
        (lanes, repr(sections["hosts"])),
    )
    # `finished_runs` drops rows that aged out of its window, so which rows it keeps at `at` is part of the key.
    window = tuple(row["run"] for row in finished_runs(records, lane_ids, at if at.tzinfo else at.replace(tzinfo=UTC)))
    runs = hold(
        "runs",
        lambda: _runs_v1(
            sections["lanes"], local_name, records, at, calls_by_run, short_ids, projects, _ends_edge(runs_dir, store_rows),
            _tasks_edge(runs_dir, task_rows, calls_by_run),
        ),
        extra=(lanes, window),
    )
    epics = hold("epics", lambda: _epics_v1(task_rows))
    return snapshot(
        now,
        chair_block,
        spend_block,
        machines,
        runs,
        queue_rows,
        queue_total,
        inbox_rows,
        inbox_total,
        [],
        _open_decisions(entries, blob),
        _spend_series(_spend_points(calls_by_run), at),
        history=history,
        history_today=today,
        chair_agents=activity["chair_agents"],
        spend_split=activity["spend_split"],
        spend_sources=activity["spend_sources"],
        budget=_budget_edge(runs_dir, at),
        epics=epics,
        board=board,
        live=live,
        orchestration=_orchestration_edge(profile, local_name, sections["hosts"], now),
        operator_sessions=activity.get("operator_sessions"),
        # From the queue rows already read (held under the store); the release is read on every refresh.
        intake=_intake_v1(task_rows, _current_release_edge()),
        # Read on every refresh, not held: the section fingerprints do not stat schedule.json.
        schedule=_schedule_edge(runs_dir),
        # Read on every refresh, not held: the section fingerprints do not stat the logs it counts.
        health=_health(work_dir, runs_dir),
    )


@dataclass(frozen=True)
class FeedCache:
    """What one cached refresh hands the next: the section memo, the fingerprints it probed, and the (cpu, wall)
    clock readings at its end."""

    memo: dash_feed_memo.Memo
    fingerprints: Mapping[str, object]
    clock: tuple[float, float]
    live: LiveCursors = LiveCursors()
    activity: chair_activity_read.Cache = MappingProxyType({})


EMPTY_CACHE = FeedCache(dash_feed_memo.EMPTY, {}, (0.0, 0.0))

# The fingerprint groups each section's builders read. Wrong belief to avoid: "machines reads only the store". Its
# lane entries come from `_board_edge`, so it reads what the board reads. Only `epics` is on the store alone.
# The `store` group is the sqlite file stat, or for a Postgres store its change stamp; it is None, so every section on
# it recomputes, only when a non-sqlite store gives no stamp.
_STORE_RECORDS_TRACES = ("store", "records", "lane_traces")
_SECTION_GROUPS = {
    "chair": ("store", "records"),
    "spend": ("store", "records"),
    "machines": _STORE_RECORDS_TRACES,
    "runs": _STORE_RECORDS_TRACES,
    "queue": _STORE_RECORDS_TRACES,
    "inbox": ("store", "records"),
    "history": _STORE_RECORDS_TRACES,
    "board": _STORE_RECORDS_TRACES,
    "epics": ("store",),
    "live": ("transcripts", "lane_traces"),
    # Internal holds: inputs `_build` shares between sections, not in `dash_feed_cost.SECTION_ORDER`, so the cost
    # block never lists them. The console gather reads the store (leases, hosts, runs, work items), the run records
    # (chair.json, pidfiles, logs) and the lane traces (each lane's calls). The review PRs read the task records
    # and the store's run hosts. The queue rows are the store's; their states add the task records under runs.
    "console": _STORE_RECORDS_TRACES,
    "review_prs": ("store", "records"),
    "queue_rows": ("store",),
    "queue_states": ("store", "records"),
    # The activity block reads the store rows, the run usage and `.ccusage-block.json` (records) and the operator
    # transcripts. It reads no lane trace.
    "activity": ("store", "records", "transcripts"),
}
# The finest step, in seconds, a section's output moves with `now` once its age fields are refreshed and its other
# time-dependent inputs are in its key (`extra` in `_build`: the lanes, the hosts, the run window, the console chair
# row). Chair counters and history count from local midnight, which falls on a minute. `spend.today` counts a UTC
# day, which falls on an hour. The rest move with `now` only through those keyed inputs.
_SECTION_STEP_S = {
    "chair": 60, "spend": 3600, "history": 60, "machines": 3600, "runs": 3600,
    "queue": 3600, "inbox": 3600, "board": 3600, "epics": 3600,
    "live": int(min(dash_live_series.MINUTE, dash_live_series.HOUR).total_seconds()),
    # The console gather compares lease expiry with `now`, and a lane whose host died lapses with no store write. Wrong
    # belief to avoid: "the console moves only with the store". Holding it for a minute at most bounds how late such a
    # lapse shows. Its chair row is read afresh on every reuse (`_console_now`), so the chair never lags. The other
    # three read no clock.
    "console": 60, "review_prs": 3600, "queue_rows": 3600, "queue_states": 3600,
    # The activity's seat spend and agent states count against `at`: a reused block is as of the minute's first refresh.
    "activity": 60,
}
# These read `courier.jsonl` and `*/initiative.md` under `work_dir`, which the probe does not stat.
_WORK_STAMPED = frozenset({"chair", "inbox"})


def _work_stamp(work_dir: Path) -> tuple:
    """Edge: (path, size, mtime_ns) of the courier log and the draft files; no size for an unreadable file."""

    def read(path: Path) -> tuple:
        try:
            info = path.stat()
        except OSError:
            return (str(path), None)
        return (str(path), info.st_size, info.st_mtime_ns)

    return (courier.stamp(work_dir / "courier.jsonl"), *(read(path) for path in sorted(work_dir.glob("*/initiative.md"))))


def _section_key(name: str, fingerprints: Mapping[str, object], at: datetime, work_stamp: tuple) -> tuple:
    """A section's memo key: its groups' fingerprints, `at` floored to its step, and the work-dir stamp where it reads
    the work dir. A group with no fingerprint gives a key equal to no other: a non-sqlite store whose change stamp
    could not be read (`run_store.store_change_stamp` gave None). A Postgres store with a stamp matches like sqlite."""
    prints = tuple(fingerprints.get(group) for group in _SECTION_GROUPS[name])
    if any(print_ is None for print_ in prints):
        return (object(),)
    return (prints, int(at.timestamp()) // _SECTION_STEP_S[name], work_stamp if name in _WORK_STAMPED else ())


def _aged_lanes(lanes: Sequence[dict], started: Mapping[str, datetime | None], at: datetime) -> list[dict]:
    """`lanes` with each `elapsed_s` counted from its run's start to `at`."""
    aware = at if at.tzinfo else at.replace(tzinfo=UTC)
    return [{**lane, "elapsed_s": dash_machine_lanes._elapsed_s(started.get(lane["run"]), aware)} for lane in lanes]


def _refresh_ages(name: str, value, anchor, at: datetime):
    """A copy of a cached section with its age fields counted to `at` from the raw stamps in `anchor`; the cached
    value is not touched. chair: `value` is `_chair_parts`' pair and `_chair_ages` redoes three ages and the liveness.
    machines: each `beat_age_s` and each lane's `elapsed_s`. board: the `elapsed_s` of its lanes by host, which
    machines reads. The other sections carry no field that moves with `at`."""
    if name == "chair":
        block, stamps = value
        return {**block, **_chair_ages(stamps, at)}, stamps
    if name == "machines":
        beats, started = anchor
        return [
            {**machine, "beat_age_s": _age_s(beat, at), "lanes": _aged_lanes(machine["lanes"], started, at)}
            for machine, beat in zip(value, beats, strict=True)
        ]
    if name == "board":
        board, lanes_by_host = value
        return board, {host: _aged_lanes(lanes, anchor, at) for host, lanes in lanes_by_host.items()}
    return value


def _store_is_local_sqlite(runs_dir: Path) -> bool:
    """Edge: whether the run store is a local sqlite file the probe can stat."""
    return not run_store.is_postgres(run_store._store_url(Path(runs_dir)))


def gather_feed_cached(
    runs_dir: Path, work_dir: Path, now: str, profile: dict | None, cache: FeedCache,
) -> tuple[dict, FeedCache]:
    """Edge: `gather_feed` plus a `cost` block, rebuilding only the store-driven sections whose inputs moved since
    `cache`. Inputs are probed once. `live` is memoised on the transcripts and lane traces fingerprints and the
    minute, and reads only bytes past the cursors in `cache.live`. The feed without `cost` equals `gather_feed`'s for
    the same inputs. Returns the feed and the cache for the next refresh."""
    start, finish = dash_feed_cost.measure(time.process_time, time.perf_counter)
    runs_dir, work_dir = Path(runs_dir), Path(work_dir)
    at = _parse_now(now)
    local_store = _store_is_local_sqlite(runs_dir)
    store_stamp = None if local_store else run_store.store_change_stamp(runs_dir)
    fingerprints = dash_feed_inputs.probe(runs_dir, Path(_PROJECTS_DIR).expanduser(), local_store, store_stamp)
    stamp = _work_stamp(work_dir)
    state: dict = {"memo": cache.memo, "live": cache.live, "activity": cache.activity}
    results: list[tuple[str, bool]] = []

    def hold(name: str, compute, anchor=None, extra=(), fresh=None):
        key = (*_section_key(name, fingerprints, at, stamp), extra)
        value, state["memo"], reused = dash_feed_memo.reuse(
            state["memo"], name, key, lambda: (compute(), None if anchor is None else anchor()),
        )
        results.append((name, reused))
        section, anchors = value
        if not reused:
            return section
        aged = _refresh_ages(name, section, anchors, at)
        return aged if fresh is None else fresh(aged)

    feed = _build(
        runs_dir, work_dir, now, profile, hold, cache.live, lambda cursors: state.update(live=cursors),
        cache.activity, lambda readers: state.update(activity=readers),
    )
    recomputed, reused = dash_feed_memo.split_reuse(dash_feed_cost.SECTION_ORDER, results)
    cpu_s, wall_s = finish()
    cost = dash_feed_cost.cost_block(cpu_s, wall_s, recomputed, reused)
    clock = (start[0] + cpu_s, start[1] + wall_s)
    return {**feed, "cost": cost}, FeedCache(state["memo"], fingerprints, clock, state["live"], state["activity"])
