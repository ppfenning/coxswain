"""Pure assembly of the versioned dash feed snapshot shape, plus the edge that
gathers a live one from the fleet's existing readers: `console_screen.gather`
for chair/spend/machines/runs, `run_store.read_queue` for the initiative
queue, and `courier.inbox` for the unacknowledged bus.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from functools import reduce
from pathlib import Path
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_tools import (
    budget,
    chair,
    chair_activity,
    chair_activity_read,
    chair_capacity,
    chair_login_check,
    chair_orchestrate,
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
    decisions,
    operator_sessions,
    pacing,
    queue_rows,
    route,
    run_store,
    runs_stranded,
    usage_meter,
    usage_window,
)
from agent_tools import (
    events as events_module,
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
from agent_tools.dash_chair_today import _ts, chair_today, local_midnight
from agent_tools.dash_cost_series import run_cost_series
from agent_tools.dash_feed_tasks import counts, task_entries
from agent_tools.dash_finished_runs import LIMIT, finished_runs
from agent_tools.dash_history import _by_run, _in_task_order, _is_landed, _landed_pr, _pr_number, _record, build_history
from agent_tools.dash_history import history_today as _history_today
from agent_tools.dash_history_store import RECENT_ENDED, read_history_rows
from agent_tools.dash_live_turns_lane import advance_lane_cursor
from agent_tools.dash_live_turns_session import read_session_turns_with_cursor
from agent_tools.dash_run_end import End, run_end
from agent_tools.dash_spend_series import spend_series as _spend_series
from agent_tools.dash_transcript_cursor import CursorState
from agent_tools.draft_list import read_drafts
from agent_tools.runs_detail import NODE_ORDER
from agent_tools.runs_top_screen import calls_from_usage

__all__ = ["EMPTY_CACHE", "FeedCache", "gather_feed", "gather_feed_cached", "snapshot"]

_LANDED_STATES = ("done", "dropped")
_LOGIN_CHECK_TIMEOUT_S = 15
_DEFAULT_PROFILE_PATH = Path.home() / ".config" / "agent-tools" / "profile.yaml"
_MAX_ROWS = 50
_LOG_TAIL_BYTES = 4096
_DECISION_PREFIX = "coxswain://decision/"
_ASK_KEYS = ("options", "context", "asked_at", "note")
# The graphs `cox route launch` starts (cli.py's route launch commands); courier `to` labels name graphs too.
_GRAPH_NAMES = ("epic", "decompose", "rescue", "cos", "sweep")
# Every seat that is neither the chair nor a person: each node in the graph roster, and each graph.
_SEATS = frozenset((*NODE_ORDER, *_GRAPH_NAMES))
_ZERO_HISTORY_TODAY = {"lands": 0, "quarantines": 0, "cost_usd": 0.0, "runs": 0}
# The default of `--projects-dir` on the stats chair command in cli.py.
_PROJECTS_DIR = "~/.claude/projects"
_ZERO_SPLIT_WINDOW = {
    "chair_session_usd": 0.0, "chair_agents_usd": 0.0, "cox_seats_usd": 0.0, "total_usd": 0.0,
    "chair_session_share": 0.0, "chair_agents_share": 0.0, "cox_seats_share": 0.0,
    "meter_fraction": 0.0, "resets_at": "", "cost_per_landed_task": 0.0,
}
# The feed's budget block when no budget is wired: every figure 0.0 or 0, period none (the feed has no nulls).
_NONE_BUDGET = {
    "period": "none", "spent_usd": 0.0, "budget_usd": 0.0, "fraction": 0.0, "daily_avg_usd": 0.0,
    "projected_usd": 0.0, "days_left": 0, "daily_avg_7d_usd": 0.0, "daily_avg_14d_usd": 0.0,
}
# What the feed says of its spend sources when the caller read none: no session named, no price catalog found.
_UNKNOWN_SPEND_SOURCES = {"chair_session": "none", "price_catalog": "missing"}
# The health block of a feed with no damaged log lines.
_ZERO_HEALTH = {"skipped_total": 0, "logs": []}


def snapshot(
    at, chair, spend, machines, runs, queue, queue_total, inbox, inbox_total, watch, decisions=(), spend_series=(),
    history=(), history_today=None, *, chair_agents=None, spend_split=None, spend_sources=None, budget=None,
    epics=(), board=None, live=None, orchestration=None, operator_sessions=None, schedule=None,
    health=None,
) -> dict:
    """Assemble the schema-1 dash feed snapshot from its sections. `queue_total`/`inbox_total` are each
    section's filtered count before the 50-row cap, for coxtop to render "50 of N". `decisions` is the open
    decision asks, newest first. `spend_series` is today's cumulative lane spend as `[at, cumulative_usd]` pairs.
    `history` is the ended-run rows and `history_today` the counts since local midnight; zeroed when not given.
    `chair_agents` is the chair's background agent rows, empty when not given; `spend_split` is the three-way
    spend split per window, all zero when not given. `spend_sources` says whether the chair session and the price
    catalog were found; it reads none and missing when not given. `budget` is the monthly budget block; it reads
    the none block, every figure 0.0 or 0, when not given. `epics` is the epic rollup records, empty when not given.
    `board` is the epics, initiatives, phases and tasks tree; it reads `{"epics": []}` when not given. `live` is the
    live section (token series, burn, agents); it reads the zero block, 60 and 24 point zero series, when not given.
    `orchestration` is the chair machine's mode block; the key is absent when it is None, because the profile or the
    hosts table could not be read. `operator_sessions` is every operator session's rows per window; the key is absent
    when it is None. `schedule` is the published schedule object, passed through unchanged; the key is absent when it
    is None. `health` is the skipped-line counts per damaged log; it reads the zero block, 0 and an empty list,
    when not given."""
    # coxswain-dash carries its own copy of tests/fixtures/dash_feed_v1.json and will need these two fields too.
    # `queue[].phases` is additive and the schema stays 1: a coxtop that does not know the field ignores it.
    # So are `runs[].tasks`, `runs[].landed` and `runs[].quarantined`, which `_runs_v1` adds to each run row.
    return {
        "schema": 1,
        "at": at,
        "chair": chair,
        "spend": spend,
        "spend_series": [list(point) for point in spend_series],
        "machines": machines,
        **({} if orchestration is None else {"orchestration": orchestration}),
        "runs": runs,
        "queue": queue,
        "queue_total": queue_total,
        "inbox": inbox,
        "inbox_total": inbox_total,
        "decisions": list(decisions),
        "watch": watch,
        "history": list(history),
        "history_today": dict(_ZERO_HISTORY_TODAY) if history_today is None else history_today,
        "chair_agents": list(chair_agents or []),
        "spend_split": (
            {name: {**_ZERO_SPLIT_WINDOW, "by_node": []} for name in ("five_hour", "weekly")}
            if spend_split is None
            else spend_split
        ),
        "spend_sources": dict(_UNKNOWN_SPEND_SOURCES) if spend_sources is None else spend_sources,
        "budget": dict(_NONE_BUDGET) if budget is None else budget,
        "epics": list(epics),
        "board": {"epics": []} if board is None else board,
        "live": _zero_live(_zero_anchor(at)) if live is None else live,
        "health": _copy_health(_ZERO_HEALTH if health is None else health),
        **({} if operator_sessions is None else {"operator_sessions": operator_sessions}),
        **({} if schedule is None else {"schedule": schedule}),
    }


def _copy_health(block: dict) -> dict:
    return {**block, "logs": [dict(row) for row in block["logs"]]}


def _zero_anchor(at) -> datetime:
    """The moment the zero live series end at: `at`, or the epoch when `at` is not a timestamp."""
    try:
        return _parse_now(at)
    except ValueError:
        return datetime.fromtimestamp(0, UTC)


_ZERO_METER = {"used_fraction": 0.0, "projected_fraction": 0.0, "ceiling_fraction": 0.0, "resets_at": "", "over_ceiling": False}
_LIVE_WINDOW = timedelta(minutes=60)


def _zero_live(at: datetime) -> dict:
    """The live block with no turns: 60 point series (24 per hour), no hosts or agents, burn all zero."""
    return {
        "throughput": dash_live_series.throughput([], at),
        "burn": {
            "usd_per_min_5m": 0.0,
            "usd_per_min_15m": 0.0,
            "meters": {name: dict(_ZERO_METER) for name in dash_live_burn.METER_NAMES},
        },
        "agents": [],
        "calls_per_min": dash_live_series.calls_per_min([], at),
        "cache_hit_pct": dash_live_series.cache_hit_pct([], at),
        "lands_per_hour": dash_live_series.per_hour([], at, 24),
        "quarantines_per_hour": dash_live_series.per_hour([], at, 24),
    }


def _parse_now(now: str) -> datetime:
    return datetime.fromisoformat(str(now).replace("Z", "+00:00"))


def _fraction(spent: float, ceiling: float | None) -> float | None:
    return None if not ceiling or ceiling <= 0 else spent / ceiling


def _profile_path() -> Path:
    """`$AGENT_TOOLS_PROFILE` when set, else `~/.config/agent-tools/profile.yaml`."""
    override = os.environ.get("AGENT_TOOLS_PROFILE")
    return Path(override).expanduser() if override else _DEFAULT_PROFILE_PATH


def _profile_ceilings() -> tuple[float | None, float | None]:
    """`(window_ceiling_usd, weekly_ceiling_usd)` from the routing profile; `(None, None)` when the file is
    missing, unreadable, or fails to parse."""
    try:
        text = _profile_path().read_text(encoding="utf-8")
    except OSError:
        return None, None
    try:
        profile = route.parse_profile(text)
    except route.ProfileError:
        return None, None
    return profile.get("window_ceiling_usd"), profile.get("weekly_ceiling_usd")


def _profile_budget() -> tuple[float | None, str]:
    """Edge. `(monthly_budget_usd, zone)` from the routing profile. The zone is `budget_timezone`, else the
    `weekly_reset` zone, else UTC; the limit is None and the zone UTC when the profile is missing or unparseable."""
    try:
        profile = route.parse_profile(_profile_path().read_text(encoding="utf-8"))
    except (OSError, route.ProfileError):
        return None, "UTC"
    reset = usage_window.parse_weekly_reset(profile.get("weekly_reset"))
    zone = profile.get("budget_timezone") or (reset.tz if reset else "UTC")
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        zone = "UTC"
    return profile.get("monthly_budget_usd"), zone


def _budget_v1(month: budget.MonthBudget, avg_7d: float, avg_14d: float) -> dict:
    """The feed's budget block. USD figures round to cents and the fraction to four places. With no limit the
    period is "none" and the limit and fraction read 0.0, but spend, projection and averages stay."""
    limited = bool(month.budget_usd)
    return {
        "period": "month" if limited else "none",
        "spent_usd": round(month.spent_usd, 2),
        "budget_usd": round(month.budget_usd, 2) if limited else 0.0,
        "fraction": round(month.fraction, 4) if limited and month.fraction is not None else 0.0,
        "daily_avg_usd": round(month.daily_avg_usd, 2),
        "projected_usd": round(month.projected_usd, 2),
        "days_left": int(month.days_left),
        "daily_avg_7d_usd": round(avg_7d, 2),
        "daily_avg_14d_usd": round(avg_14d, 2),
    }


def _budget_edge(runs_dir: Path, at: datetime) -> dict:
    """Edge. The budget block for `at`'s month, spend read from the store with the usage files as fallback.
    Reads the profile file as `_spend` does, not `gather_feed`'s `profile` argument. A read failure yields the
    none block: a secondary block must not take the whole feed down."""
    limit, zone = _profile_budget()
    runs_dir = Path(runs_dir)

    def cost_between(since: str, until: str) -> float | None:
        return run_store.cost_since(runs_dir, since, until)

    try:
        month = usage_window.gather_month(
            runs_dir, at, zone, limit, cost_between,
            min_elapsed_fraction=_pacing_policy(runs_dir).min_elapsed_fraction,
        )
        daily = usage_window.gather_daily_costs(runs_dir, at, zone, 14, cost_between)
    except (OSError, ValueError, RuntimeError):
        return dict(_NONE_BUDGET)
    today = at.astimezone(ZoneInfo(zone)).date()
    return _budget_v1(month, budget.rolling_daily_avg(daily, today, 7), budget.rolling_daily_avg(daily, today, 14))


def _pacing_policy(runs_dir: Path) -> pacing.Policy:
    """The pacing policy the chair loop enforces, from the loader it uses on the same runs directory."""
    # Deferred: cli.py imports this module, so a top-level import of cli is circular.
    from agent_tools import cli

    return cli._resolved_pacing_policy(Path(runs_dir))


def _hard_stop_fraction(runs_dir: Path) -> float:
    """The weekly hard stop the chair loop enforces."""
    return _pacing_policy(runs_dir).weekly_hard_stop_fraction


def _weekly_pace_keys(weekly: pacing.Window | None, policy: pacing.Policy, at: datetime) -> dict:
    """The spend header's weekly target and verdict. The target reads 0.0 whenever the pace is not judged."""
    pace = pacing.weekly_pace(weekly, policy, at)
    judged = pace.verdict != "not_judged" and pace.target_fraction is not None
    return {
        "weekly_target_fraction": pace.target_fraction if judged else 0.0,
        "weekly_pace": pace.verdict,
    }


def _profile_weekly_reset() -> usage_window.WeeklyReset | None:
    """Edge. The routing profile's `weekly_reset`, parsed as the chair parses it; None when the profile is
    missing, unreadable or unparseable, or sets no reset."""
    try:
        profile = route.parse_profile(_profile_path().read_text(encoding="utf-8"))
    except (OSError, route.ProfileError):
        return None
    return usage_window.parse_weekly_reset(profile.get("weekly_reset"))


def _store_meter(read_store, runs_dir: Path) -> usage_meter.Meter | None:
    """Edge. The newest meter the store holds, through `read_store` (`run_store.latest_chair_meter`, the reader
    the standby chair's "(meter, store)" label uses). None for no row, a store error, or a naive `observed_at`,
    which `usage_meter.fresh` cannot compare with an aware clock."""
    try:
        doc = read_store(Path(runs_dir))
    except (*run_store._db_errors(), ValueError, TypeError, RuntimeError):  # RuntimeError: psycopg not installed
        return None
    meter = usage_meter.parse(doc) if doc else None
    return meter if meter is not None and meter.observed_at.tzinfo is not None else None


def _newest_meter(*meters: usage_meter.Meter | None) -> usage_meter.Meter | None:
    """The meter with the latest `observed_at`, even a stale one; None when there is none."""
    seen = [m for m in meters if m is not None and m.observed_at.tzinfo is not None]
    return max(seen, key=lambda m: m.observed_at, default=None)


def _meter_week(meter: usage_meter.Meter) -> pacing.Window:
    """The seven days ending at the meter's weekly reset, spent as a fraction of a ceiling of 1.0."""
    reset = meter.seven_day.resets_at
    return pacing.Window(
        start=reset - timedelta(days=7), end=reset, spent_usd=meter.seven_day.used_percentage / 100,
        ceiling_usd=1.0, burn_usd_per_hour=0.0, runs_in_flight=0,
    )


def _from_meter(
    meter: usage_meter.Meter, label: str, spend_source: str, at: datetime, hard_stop: float, policy: pacing.Policy,
) -> dict:
    five = meter.five_hour
    # No five-hour limit on this plan: the feed has no nulls, so it reads 0.0, source "none" and no reset time.
    return {
        "five_hour_fraction": 0.0 if five is None else five.used_percentage / 100,
        "five_hour_source": "none" if five is None else label,
        "five_hour_resets_at": "" if five is None else five.resets_at.isoformat(),
        "weekly_fraction": meter.seven_day.used_percentage / 100,
        "weekly_source": label,
        "weekly_resets_at": meter.seven_day.resets_at.isoformat(),
        "hard_stop_fraction": hard_stop,
        "spend_source": spend_source,
        "spend_age_s": _age_s(meter.observed_at.isoformat(), at),
        **_weekly_pace_keys(_meter_week(meter), policy, at),
    }


def _spend(runs_dir: Path, now: str, *, read_local=None, read_store=None, read_reset=None) -> dict:
    """The spend header, from the first of three sources: a fresh local usage meter; else the newest meter in
    the store when fresh; else an estimate. The weekly estimate is ceilinged by an implied ceiling the meter has
    recorded, falling back to the routing profile's own, and starts at the newest meter's reset (even a stale
    one), else the profile's configured reset. The five-hour estimate starts at the newest meter's five-hour
    window, rolled forward to under five hours old, while that meter is under five hours old; with an older meter or
    none it takes the active ccusage block (the five hours before now without one). It is always ceilinged by the
    profile's window ceiling.
    `spend_source` is "local meter", "meter store" or "estimate"; `spend_age_s` is the age of the meter that
    answered, or of the newest stale one an estimate went on, and None when no meter exists.
    `weekly_target_fraction` and `weekly_pace` are the pacing policy's weekly verdict: over the seven days ending
    at the meter's reset on the meter paths, over the gathered weekly window on the estimate path.
    `read_local`, `read_store` and `read_reset` default to the real readers, looked up at call time."""
    at = _parse_now(now)
    policy = _pacing_policy(runs_dir)
    hard_stop_fraction = _hard_stop_fraction(runs_dir)
    local = (usage_meter.read if read_local is None else read_local)()
    if local is not None and usage_meter.fresh(local, at):
        return _from_meter(local, "meter", "local meter", at, hard_stop_fraction, policy)
    stored = _store_meter(run_store.latest_chair_meter if read_store is None else read_store, runs_dir)
    if stored is not None and usage_meter.fresh(stored, at):
        return _from_meter(stored, "meter, store", "meter store", at, hard_stop_fraction, policy)
    newest = _newest_meter(local, stored)
    reset = (_profile_weekly_reset if read_reset is None else read_reset)()
    start = usage_window.weekly_start_from_reset(
        None if newest is None else newest.seven_day.resets_at,
        None if reset is None else usage_window.weekly_window_start(at, reset),
        at,
    )
    # A meter older than one five-hour window says nothing about the current one: let `gather` take the active
    # ccusage block, as `cox usage assess` does, instead of summing from a reset hours or days gone.
    five_start = (
        None
        if newest is None or newest.five_hour is None or at - newest.observed_at >= timedelta(hours=5)
        else usage_window.five_hour_start(newest.five_hour.resets_at, at)
    )
    window_ceiling, profile_weekly_ceiling = _profile_ceilings()
    weekly_ceiling = usage_meter.implied_ceiling("weekly", at)
    if weekly_ceiling is None:
        weekly_ceiling = profile_weekly_ceiling
    window = usage_window.gather(runs_dir, at, ceiling_usd=window_ceiling, start=five_start)
    weekly = usage_window.gather_weekly(runs_dir, at, weekly_ceiling_usd=weekly_ceiling, start=start)
    return {
        "five_hour_fraction": _fraction(window.spent_usd, window.ceiling_usd),
        "five_hour_source": "est",
        "five_hour_resets_at": None,
        "weekly_fraction": _fraction(weekly.spent_usd, weekly.ceiling_usd),
        "weekly_source": "est",
        "weekly_resets_at": None,
        "hard_stop_fraction": hard_stop_fraction,
        "spend_source": "estimate",
        "spend_age_s": None if newest is None else _age_s(newest.observed_at.isoformat(), at),
        **_weekly_pace_keys(weekly, policy, at),
    }


def _window_starts(spend: Mapping, at: datetime) -> tuple[datetime, datetime]:
    """The five-hour and weekly window starts: 5 hours and 7 days before each `resets_at`, or before `at` with none."""
    five = (_utc(spend.get("five_hour_resets_at")) or at) - timedelta(hours=5)
    weekly = (_utc(spend.get("weekly_resets_at")) or at) - timedelta(days=7)
    return five, weekly


def _landed_at(task: Mapping, ended: Mapping[str, datetime | None]) -> datetime | None:
    """When a landed task record landed: its `landed.at`, else the end of its run."""
    landed = _record(task.get("record_json")).get("landed")
    stamp = landed.get("at") if isinstance(landed, Mapping) else None
    return _utc(stamp) or ended.get(str(task.get("run_id")))


def _window_node_inputs(rows: Mapping, windows: Mapping[str, Mapping]) -> tuple[dict[str, list], dict[str, int]]:
    """Each window's `node_calls` rows at or after its `start`, and its landed task records likewise.
    `rows` holds ended runs only, so a run still in flight is not counted."""
    ended = {str(run.get("run_id")): _utc(run.get("ended_at")) for run in rows.get("runs", ())}
    calls = [(_utc(call.get("ts")), call) for call in rows.get("node_calls", ())]
    lands = [_landed_at(task, ended) for task in rows.get("task_records", ()) if _is_landed(task)]
    starts = {name: _utc(window["start"]) for name, window in windows.items()}
    node_rows = {name: [call for ts, call in calls if ts and start and ts >= start] for name, start in starts.items()}
    done = {name: sum(1 for ts in lands if ts and start and ts >= start) for name, start in starts.items()}
    return node_rows, done


def _pane_meter_fraction(spend: Mapping, window: str) -> float:
    """The meter's own fraction for `window` ("five_hour" or "weekly"); 0.0 when `spend` is an estimate, whose
    fraction covers a different window than the pane's `start` and `seat_usd`. The feed has no nulls, and the
    estimate's empty `resets_at` already tells a consumer no meter was read."""
    return 0.0 if spend.get(f"{window}_source") == "est" else float(spend.get(f"{window}_fraction") or 0.0)


def _workspace(work_dir: Path) -> Path:
    """The workspace root: the parent of a directory named `work`, else `work_dir` itself."""
    return work_dir.parent if work_dir.name == "work" else work_dir


def _activity(
    runs_dir: Path, profile: dict, session: str, workspace: Path, spend: Mapping, at: datetime,
    store_rows: Mapping | None = None,
) -> dict:
    """Edge: the chair's background agent rows, the three-way spend split, `operator_sessions` and `spend_sources`.
    The sessions are every transcript in `workspace`'s project folder plus the lease holder's own when it resolves
    (status ok), even if its transcript sits in another project folder; an unresolved holder adds none, so
    `sessions_found` counts transcripts read. `chair_agents` are the holder's agents, empty for a service chair.
    `spend_sources` says whether the holder's session and the price catalog were found, and the session counts. Each
    window starts five hours (weekly: seven days) before its `resets_at`, or before `at` when the spend section has
    no reset time to read. The per-node rows and done counts are cut from `store_rows`, the one store read, which
    reaches back to the weekly start."""
    projects_dir = Path(_PROJECTS_DIR).expanduser()
    five_start, weekly_start = _window_starts(spend, at)
    windows = {
        "five_hour": {
            "start": five_start.isoformat(),
            "seat_usd": usage_window.gather(runs_dir, at, ceiling_usd=None).spent_usd,
            "meter_fraction": _pane_meter_fraction(spend, "five_hour"),
            "resets_at": spend.get("five_hour_resets_at") or "",
        },
        "weekly": {
            "start": weekly_start.isoformat(),
            "seat_usd": usage_window.gather_weekly(
                runs_dir, at, weekly_ceiling_usd=None, start=weekly_start,
            ).spent_usd,
            "meter_fraction": _pane_meter_fraction(spend, "weekly"),
            "resets_at": spend.get("weekly_resets_at") or "",
        },
    }
    node_rows, done_counts = _window_node_inputs(store_rows or {}, windows)
    holder_lines = chair_activity_read.read_session_lines(projects_dir, session)
    holder_agents = chair_activity_read.read_agents(projects_dir, session)
    holder_status = chair_activity_read.session_status(projects_dir, session)
    prices = chair_activity_read.prices_for_profile(profile)
    # A holder that is not_found or ambiguous has no transcript to add; `with_holder` adds only a non-empty id.
    sessions = operator_sessions.with_holder(
        chair_activity_read.read_project_sessions(projects_dir, workspace),
        session if holder_status == "ok" else "",
        holder_lines,
        holder_agents,
    )
    gathered = chair_activity.gather_activity(
        holder_lines,
        holder_agents,
        prices,
        at,
        windows,
        node_rows=node_rows,
        done_counts=done_counts,
        sessions=sessions,
    )
    sources = {
        "chair_session": holder_status,
        "price_catalog": chair_activity_read.catalog_status(profile),
        **operator_sessions.sources(sessions, prices),
    }
    return {**gathered, "spend_sources": sources}


def _local_identity(runs_dir: Path, profile: dict) -> tuple[str, int]:
    """This machine's own name and the lane capacity `cox chair run` enforces, from `chair_capacity`."""
    return socket.gethostname(), chair_capacity.chair_max_in_flight(Path(runs_dir), profile)


def _orchestration_v1(chair_host: str, configured: str, rows: Sequence[Mapping], now: str) -> dict:
    """The chair machine's orchestration block: the lane hosts are the active hosts other than the chair's own."""
    lane_hosts = [name for name in chair_orchestrate.active_hosts(rows, now) if name != chair_host]
    return {
        "chair_host": chair_host,
        "configured": configured,
        "mode": chair_orchestrate.mode_name(configured, lane_hosts),
        "available": chair_orchestrate.orchestration_available(rows, now),
    }


def _orchestration_edge(profile: Mapping | None, chair_host: str, rows: Sequence[Mapping], now: str) -> dict | None:
    """Edge: the orchestration block, or None when the profile or the hosts rows cannot be read."""
    try:
        if not isinstance(profile, dict):
            return None
        return _orchestration_v1(chair_host, chair_capacity.chair_local_lanes(profile), rows, now)
    except Exception:  # the block is garnish: an unreadable profile or table must not take the feed down
        return None


def _run_capturing(argv: list[str]) -> tuple[int, str]:
    """Edge. `(exit code, stdout and stderr joined)`; `(1, "")` when the command is missing or times out."""
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=_LOGIN_CHECK_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return done.returncode, done.stdout + done.stderr


def _local_login(local_name: str) -> bool | None:
    """Edge. Whether `claude auth status` says this machine is logged in, as `chair_login_check` reads it for
    a host named `socket.gethostname()`: True or False when the reply says so, None when it cannot be read."""
    return chair_login_check._login_ok(local_name, "", _run_capturing)


def _local_versions(login_ok: bool | None, now: str) -> dict | None:
    """The local row's `versions_json` after a login check at `now`; None when the check could not tell, so
    the row stays unchecked and reads as unknown."""
    return None if login_ok is None else {"login_ok": login_ok, "login_checked_at": now}


def _group_rows(rows: Iterable[Mapping]) -> dict[Any, list[Mapping]]:
    """`rows` by their `initiative` in one pass, row order kept within each group. A row naming none sits under `None`.
    The per-initiative helpers filter `rows` themselves, so handing each its own group gives the same result without
    a scan of every row per initiative."""
    groups: dict[Any, list[Mapping]] = {}  # mutation localised to this one pass
    for row in rows:
        groups.setdefault(row.get("initiative"), []).append(row)
    return groups


def _initiative_progress(rows: list[dict], initiative: str) -> tuple[int, int, str | None]:
    """`(phases_landed, phases_total, current_phase)` for one initiative's task rows: a phase is landed when
    every one of its items is `done` or `dropped`, the same rule `console_screen._phase_progress` uses.
    `current_phase` is the first not-yet-landed phase in sorted order, or the last phase once all have landed."""
    by_phase: dict[str, list[str]] = {}
    for row in rows:
        if row.get("kind") == "task" and row.get("initiative") == initiative:
            by_phase.setdefault(row.get("phase"), []).append(row.get("state"))
    phases = sorted(by_phase)
    landed_flags = {phase: bool(states) and all(s in _LANDED_STATES for s in states) for phase, states in by_phase.items()}
    landed = sum(1 for ok in landed_flags.values() if ok)
    current = next((phase for phase in phases if not landed_flags[phase]), phases[-1] if phases else None)
    return landed, len(phases), current


def _epics_v1(rows: list[dict], definitions: Mapping[str, Mapping] | None = None) -> list[dict]:
    """The feed's `epics`: one record per epic that has member initiatives, sorted by epic id, finished or not.
    An initiative is landed when it has phases and all of them landed. `definitions` maps an epic id to its
    `title`; the id is the title when none is given."""
    from core.epics import rollup  # lazy: core sits beside agent_tools in one venv

    initiatives = sorted((row for row in rows if row.get("kind") == "initiative" and row.get("initiative")), key=lambda r: r["initiative"])
    members = {row["initiative"]: queue_rows.epic_of_row(row) for row in initiatives}
    groups = _group_rows(rows)
    progress = {i: _initiative_progress(groups.get(i, []), i) for i in members}
    landed = [i for i, (done, total, _) in progress.items() if total > 0 and done == total]
    # Members may name different branches; the epic takes the first one set, in initiative-id order.
    branches: dict[str, str] = {}
    for row in initiatives:
        branch = queue_rows.target_branch_of_row(row)
        epic = members[row["initiative"]]
        if epic is not None and branch is not None:
            branches.setdefault(epic, branch)
    defs = {
        epic: {"title": (definitions or {}).get(epic, {}).get("title"), "target_branch": branches.get(epic)}
        for epic in set(members.values()) - {None}
    }
    return [
        {
            **{key: record[key] for key in ("id", "title", "target_branch", "landed", "total", "complete")},
            "initiatives": [
                {**entry, "phases_landed": progress[entry["id"]][0], "phases_total": progress[entry["id"]][1]}
                for entry in record["initiatives"]
            ],
        }
        for record in rollup(members, landed, defs)
    ]


def _configured_land_at(rows: Sequence[Mapping], initiative: str) -> str:
    """The granularity landing is configured at for an initiative: its initiative row's `extra["land_at"]`, else `ticket`."""
    return next(
        (
            row["extra"]["land_at"]
            for row in rows
            if row.get("kind") == "initiative" and row.get("initiative") == initiative
            and isinstance(row.get("extra"), Mapping) and row["extra"].get("land_at")
        ),
        "ticket",
    )


def _landed_word(task: Mapping, land_at: str | None) -> str:
    """A `done` row reads `landed` when its `extra["land_at"]` equals the configured one or it names none; a done row
    written at another granularity is merged work that has not landed at the configured one, so it reads `merged`."""
    extra = task.get("extra")
    written = extra.get("land_at") if isinstance(extra, Mapping) else None
    return "landed" if written is None or land_at is None or written == land_at else "merged"


def _task_state(
    task: Mapping, landed: Collection[str], on_live_lane: bool, stranded: bool, land_at: str | None = None,
) -> str | None:
    """The feed state of a queue task row, or `None` for a store state the feed has no word for (`todo`, unknown).
    `blocked` is a ready task with a need outside `landed` (`route.TERMINAL`, the rule `chair_read_idle_backlog`
    uses for the relaunch's "waits on" line) or a store-`blocked` hold. `building` needs a live lane over the
    task's initiative and phase: an `in_progress` row with none is a dead build and reads `blocked`. A launched
    task stays `ready` in the store for the whole run, so a `ready` row with every need landed reads `building`
    on a live lane. The lane covers a phase, not one task, so its idle siblings read `building` as well.
    `stranded` marks a task `runs stranded` lists, which reads `approved` even while the store still says `ready`.
    `approved` with the merge flag (`route.is_merged`) reads `merged`. `done` reads `landed` only at the
    configured `land_at` (`_landed_word`), else `merged`."""
    state = task.get("state")
    if state == "done":
        return _landed_word(task, land_at)
    if state in ("dropped", "quarantined"):
        return state
    if state == "approved" and route.is_merged(task):
        return "merged"
    if state == "approved" or stranded:
        return "approved"
    if state == "in_progress":
        return "building" if on_live_lane else "blocked"
    if state == "ready":
        if not all(need in landed for need in task.get("needs") or ()):
            return "blocked"
        return "building" if on_live_lane else "ready"
    return "blocked" if state == "blocked" else None


def _own_tasks(rows: Sequence[Mapping], initiative: str) -> list[Mapping]:
    """The queue's task rows for one initiative, in row order."""
    return [row for row in rows if row.get("kind") == "task" and row.get("initiative") == initiative]


def _task_states(
    rows: Sequence[Mapping], initiative: str, live_phases: Collection[tuple[str, str]],
    stranded: Collection[tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    """`(phase, task_id, state)` for each of the initiative's tasks in row order, the state `_task_state` gives.
    A task with no feed state is left out. `live_phases` holds `(initiative, phase)` per live lane, a blank
    phase covering the whole initiative (a lane that has not written a phase yet). `stranded` holds
    `(initiative, phase, task)` per task `runs stranded` lists."""
    own = _own_tasks(rows, initiative)
    landed = {row.get("task_id") for row in own if row.get("state") in route.TERMINAL}
    lane_phases = {phase for lane_initiative, phase in live_phases if lane_initiative == initiative}
    land_at = _configured_land_at(rows, initiative)
    return [
        (row.get("phase"), row.get("task_id"), state)
        for row in own
        if (
            state := _task_state(
                row, landed, bool(lane_phases) and ("" in lane_phases or row.get("phase") in lane_phases),
                (initiative, row.get("phase"), row.get("task_id")) in stranded, land_at,
            )
        )
        is not None
    ]


def _initiative_phases(
    rows: Sequence[Mapping], initiative: str, live_phases: Collection[tuple[str, str]],
    stranded: Collection[tuple[str, str, str]],
) -> list[dict]:
    """The initiative's tasks as `[{"id": phase, "tasks": [{"id", "state"}, ...]}]`, phases in sorted order (the
    order `_initiative_progress` uses) and tasks in row order. Dropped tasks stay listed, marked `dropped`; a task
    with no feed state is left out (`_task_states`)."""
    states = _task_states(rows, initiative, live_phases, stranded)
    return [
        {"id": phase, "tasks": [{"id": task, "state": state} for p, task, state in states if p == phase]}
        for phase in sorted({row.get("phase") for row in _own_tasks(rows, initiative)})
    ]


def _queue_states(
    rows: Sequence[Mapping], live_phases: Collection[tuple[str, str]], stranded: Collection[tuple[str, str, str]],
) -> dict[tuple[str, str, str], str]:
    """Every queue task's feed state by `(initiative, phase, task_id)`, the state `phases[].tasks[].state` shows."""
    return {
        (initiative, phase, task): state
        for initiative, own in _group_rows(rows).items()
        for phase, task, state in _task_states(own, initiative, live_phases, stranded)
    }


def _task_records(runs_dir: Path) -> list[dict]:
    """Edge. The task records `cox runs stranded` reads (`cli._stranded_inputs`): `<run>/tasks/<phase>/<task>.json`,
    with run, task and phase defaulted from the path. An unreadable record is skipped."""
    records = []
    for path in sorted(runs_dir.glob("*/tasks/*/*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            records.append({"run": path.parent.parent.parent.name, "task": path.stem, "phase": path.parent.name, **record})
    return records


def _queue_rows_edge(runs_dir: Path) -> list[dict]:
    """Edge: the store's queue rows, read once for the history's task states and the runs' task lists. An unreadable
    store (`OSError`) gives none; any other error is a fault and is raised."""
    try:
        return run_store.read_queue(runs_dir)
    except OSError:
        return []


def _queue_states_edge(
    runs_dir: Path, live_phases: Collection[tuple[str, str]], rows: Sequence[dict],
) -> dict[tuple[str, str, str], str]:
    """Edge: `_queue_states` over the queue `rows` (read once by `gather_feed`, which also gives them to the runs' task
    lists). An unreadable store gives no states, so history rows list no tasks."""
    try:
        return _queue_states(rows, live_phases, _stranded_tasks(_task_records(runs_dir), rows))
    except Exception:  # the task states are garnish: a store failure must not take the feed down
        return {}


def _stranded_tasks(records: Sequence[dict], rows: Sequence[Mapping]) -> frozenset[tuple[str, str, str]]:
    """`(initiative, phase, task)` of every queue task `runs_stranded` pairs with an approved, unlanded record.
    Uses its `_live_pairs` so the feed and `cox runs stranded` share one rule; the repo and branch filters of
    `stranded` only decide the remedy line and are not applied. A record that names no one item is left out."""
    items = [
        {"id": row.get("task_id"), "initiative": row.get("initiative"), "phase": row.get("phase"), "state": row.get("state")}
        for row in rows
        if row.get("kind") == "task"
    ]
    return frozenset(
        (item["initiative"], item["phase"], item["id"])
        for _, item in runs_stranded._live_pairs(list(records), items)
        if item is not None
    )


def _live_phases(lanes: Sequence[console_screen.LaneRow]) -> frozenset[tuple[str, str]]:
    """`(initiative, phase)` per live lane, local or remote: a lane is live by its store lease heartbeat."""
    return frozenset((_run_slug(lane.run), lane.phase) for lane in lanes)


def _live_lanes(lanes: Sequence[console_screen.LaneRow]) -> dict[tuple[str, str], str]:
    """`(initiative, phase)` to the run id of the live lane covering it. Its keys are `_live_phases`, so a reader of
    plain pairs takes it unchanged; `_queue` adds the wave keys only when given this map. Two lanes on one pair keep the
    lowest run id."""
    return {(_run_slug(lane.run), lane.phase): lane.run for lane in sorted(lanes, key=lambda lane: lane.run, reverse=True)}


# `_phase_parents` and `_compute_waves` repeat harness.waves.phase_parents and compute_waves (src/graphs/harness/waves.py).
# agent_tools never imports harness at module level (cli.py imports it inside the commands that need it), and the feed
# must not fail to start where harness is absent, so the few lines are copied. Change one, change the other.
def _phase_parents(items: Sequence[Mapping]) -> dict[str, set[str]]:
    """Phase B has parent A when a task in B needs a task in A that is not done; a done need adds none."""
    phase_of = {str(item["id"]): str(item.get("phase") or "") for item in items}
    state_of = {str(item["id"]): item.get("state") for item in items}
    parents: dict[str, set[str]] = {phase: set() for phase in phase_of.values() if phase}
    for item in items:
        here = str(item.get("phase") or "")
        for need in item.get("needs") or []:
            there = phase_of.get(str(need))
            if here and there and there != here and state_of.get(str(need)) != "done":
                parents[here].add(there)
    return parents


def _compute_waves(parents: Mapping[str, set[str]]) -> tuple[list[list[str]], list[str]]:
    """Wave n holds the phases whose parents all sit in earlier waves; phases in a cycle are returned apart."""
    waves: list[list[str]] = []
    placed: set[str] = set()
    while True:
        ready = sorted(p for p in parents if p not in placed and parents[p] <= placed)
        if not ready:
            return waves, sorted(set(parents) - placed)
        waves = [*waves, ready]
        placed = placed | set(ready)


def _task_settled(row: Mapping) -> bool:
    """Landed or merged: `done`, `dropped`, or `approved` with the merge flag (`_task_state` reads these as landed,
    dropped or merged)."""
    return row.get("state") in route.TERMINAL or (row.get("state") == "approved" and route.is_merged(row))


def _phase_status(
    tasks: Sequence[Mapping], lane: str | None, parents: Collection[str], parent_status: Mapping[str, str],
) -> tuple[str, str | None]:
    """`(status, reason)` of one phase from its task rows, its live lane's run id and its parents' statuses.
    `running` while a lane is live. `blocked` for a quarantined task (the reason names it and its own `reason`) or,
    once none of its own work remains, a blocked parent. `merged` when every task is settled and one is approved
    and merged into the pr branch; `complete` when every task is done or dropped. A merged parent gates its
    children like a complete one. Else `waiting`, whose reason names the parents not yet finished, or is None."""
    quarantined = next((task for task in tasks if task.get("state") == "quarantined"), None)
    blocked_parent = next((parent for parent in sorted(parents) if parent_status.get(parent) == "blocked"), None)
    unfinished = [parent for parent in sorted(parents) if parent_status.get(parent) not in ("complete", "merged")]
    if lane is not None:
        return "running", None
    if quarantined is not None:
        why = str(quarantined.get("reason") or "")
        return "blocked", f"task {quarantined.get('task_id')} is quarantined" + (f": {why}" if why else "")
    if tasks and all(_task_settled(task) for task in tasks):
        merged = any(task.get("state") == "approved" and route.is_merged(task) for task in tasks)
        return ("merged" if merged else "complete"), None
    if blocked_parent is not None:
        return "blocked", f"parent {blocked_parent} is blocked"
    return "waiting", (f"waiting on {', '.join(unfinished)}" if unfinished else None)


def _waved(row: dict, rows: Sequence[Mapping], lanes: Mapping[tuple[str, str], str]) -> dict:
    """The queue row with `waves` (`[{"n", "phases"}]`), `widest_wave`, and `wave`, `lane`, `status`, `reason` on each
    of its phases. A phase in a dependency cycle has no wave (null). Statuses are settled wave by wave, so a phase
    reads its parents' statuses. `lanes` is `_live_lanes`; a lane with a blank phase covers every phase."""
    initiative = row["initiative"]
    own = _own_tasks(rows, initiative)
    parents = _phase_parents(
        [{"id": r.get("task_id"), "phase": r.get("phase"), "state": r.get("state"), "needs": r.get("needs")} for r in own],
    )
    waves, cyclic = _compute_waves(parents)
    wave_of = {phase: n for n, group in enumerate(waves, 1) for phase in group}
    unplaced = [p["id"] for p in row["phases"] if p["id"] not in parents]
    lane_of = {p["id"]: lanes.get((initiative, p["id"])) or lanes.get((initiative, "")) for p in row["phases"]}

    def settle(done: Mapping[str, tuple[str, str | None]], phase: str) -> dict[str, tuple[str, str | None]]:
        tasks = [r for r in own if r.get("phase") == phase]
        status = _phase_status(tasks, lane_of.get(phase), parents.get(phase, ()), {p: s for p, (s, _) in done.items()})
        return {**done, phase: status}

    settled = reduce(settle, [*(p for group in waves for p in group), *cyclic, *unplaced], {})
    return {
        **row,
        "waves": [{"n": n, "phases": group} for n, group in enumerate(waves, 1)],
        "widest_wave": max(map(len, waves), default=0),
        "phases": [
            {
                "id": p["id"], "wave": wave_of.get(p["id"]), "lane": lane_of[p["id"]],
                "status": settled[p["id"]][0], "reason": settled[p["id"]][1], "tasks": p["tasks"],
            }
            for p in row["phases"]
        ],
    }


def _priority(rows: list[dict], initiative: str):
    """The first `priority` an initiative's own rows carry in `extra` (arbitrary frontmatter passed through
    by `queue_rows.parse_item`); `None` when no row names one."""
    return next(
        (
            row["extra"]["priority"]
            for row in rows
            if row.get("initiative") == initiative and isinstance(row.get("extra"), dict) and "priority" in row["extra"]
        ),
        None,
    )


def _priority_key(priority) -> tuple[int, int]:
    """Sort key for a `priority` value: numeric values ascending (1 first), anything absent or non-numeric last."""
    try:
        return (0, int(priority))
    except (TypeError, ValueError):
        return (1, 0)


def _queue(runs_dir: Path, live_phases: Collection[tuple[str, str]] = ()) -> tuple[list[dict], int]:
    """Initiatives the store's queue names with at least one phase not landed (a fully landed initiative drops
    out entirely), ordered by priority, then by first appearance in `read_queue`'s row order. Capped at
    `_MAX_ROWS`; the second element is the filtered count before the cap. Each row lists its `phases` with
    every task's state; `live_phases` is `_live_phases` of the live lanes. Given `_live_lanes` instead (a mapping, whose
    keys are those pairs) each row also carries `waves`, `widest_wave` and the per-phase wave keys (`_waved`); plain
    pairs carry no run ids, so they give the rows as before."""
    # Not a verified age. Queue rows carry no timestamp (`run_store._QUEUE_COLUMNS`), and the
    # `harness.store_queue read` contract (run_store.py:1009) promises no row order. Row order is age only
    # if the harness returns rows in insertion order, and that is unknown from this repository.
    rows = run_store.read_queue(runs_dir)
    stranded = _stranded_tasks(_task_records(runs_dir), rows)
    # Walked backwards, so the last write for each initiative is its first index.
    first_seen = {row["initiative"]: index for index, row in reversed(list(enumerate(rows))) if row.get("initiative")}
    groups = _group_rows(rows)
    progress = {initiative: _initiative_progress(groups[initiative], initiative) for initiative in first_seen}
    priorities = {initiative: _priority(groups[initiative], initiative) for initiative in first_seen}
    open_initiatives = sorted(
        (initiative for initiative in first_seen if progress[initiative][0] < progress[initiative][1]),
        key=lambda initiative: (_priority_key(priorities[initiative]), first_seen[initiative]),
    )
    queue = [
        {
            "initiative": initiative,
            "priority": priorities[initiative],
            "phases_landed": progress[initiative][0],
            "phases_total": progress[initiative][1],
            "current_phase": progress[initiative][2],
            "phases": _initiative_phases(groups[initiative], initiative, live_phases, stranded),
        }
        for initiative in open_initiatives
    ]
    shown = queue[:_MAX_ROWS]
    return ([_waved(row, groups[row["initiative"]], live_phases) for row in shown] if isinstance(live_phases, Mapping) else shown), len(queue)


def _reaches_a_person(to: str) -> bool:
    """True for `chair` (or a `chair-*` label, docs/design/courier.md's Labels section) or a person; false for
    any node or graph in `_SEATS`. Courier keeps no person registry, so a label outside `_SEATS` reads as a person."""
    return to == "chair" or to.startswith("chair-") or to not in _SEATS


def _courier_blob(work_dir: Path) -> str:
    """The raw courier.jsonl; "" when it cannot be read."""
    try:
        return (Path(work_dir) / "courier.jsonl").read_text(encoding="utf-8")
    except OSError:
        return ""


def _inbox(entries: list[dict]) -> tuple[list[dict], int]:
    """The `entries` addressed to `chair` or to a person, newest first (any other seat drops out entirely).
    With no label `courier.inbox` never consults the lock holder, so the chair lock is not read. Capped at
    `_MAX_ROWS`; the second element is the filtered count before the cap."""
    # `courier.inbox` preserves each id's first-appearance (oldest-first) order; reverse for newest first.
    reached = [e for e in entries if _reaches_a_person(str(e.get("to") or ""))][::-1]
    return reached[:_MAX_ROWS], len(reached)


def _is_decision(entry: dict) -> bool:
    return str(entry.get("ref") or "").startswith(_DECISION_PREFIX)


def _open_decisions(entries: list[dict], blob: str) -> list[dict]:
    """Unacknowledged asks in `entries` with no answer anywhere in `blob`, newest first. An answer carries
    `answer`; an ask carries `_ASK_KEYS`, and a decision entry with neither is left out."""
    asks = [e for e in entries if _is_decision(e) and "answer" not in e and all(key in e for key in _ASK_KEYS)]
    # An acked answer drops out of `courier.inbox`, so answers come from every line of the blob, acked or not.
    lines = [json.loads(line) for line in blob.splitlines() if line.strip()]
    answers = [e for e in lines if _is_decision(e) and "answer" in e]
    return decisions.open_decisions_newest_first(decisions.merge_decisions(asks, answers))


def _age_s(text, at: datetime) -> int:
    """Whole seconds from an ISO `text` to `at`, never negative; 0 when `text` is missing or unreadable."""
    try:
        return max(0, int((at - _parse_now(text)).total_seconds()))
    except (TypeError, ValueError):
        return 0


def _tick_age_s(last_tick_at: str | None, at: datetime) -> int | None:
    """Whole seconds from the status line's stamp (`MM-DD HH:MM TZ`, Eastern, no year) to `at`; None when absent or unreadable.

    The stamp carries no year, so it reads in `at`'s year, or the year before when that would be later than `at`."""
    local = at.astimezone(EASTERN)
    for year in (local.year, local.year - 1):
        try:
            then = datetime.strptime(f"{year} {(last_tick_at or '')[:11]}", "%Y %m-%d %H:%M").replace(tzinfo=EASTERN)
        except ValueError:
            continue
        if then <= at:
            return int((at - then).total_seconds())
    return None


def _chair_v1(
    row: dict, lease: dict, record: dict, at: datetime,
    status: tuple[str | None, str | None] = (None, None),
    action: dict | None = None,
    action_rows: list[dict] | None = None,
    needs_chair_open: int = 0,
    last_housekeeping_at: str | None = None,
    inbox_entries: list[dict] | None = None,
    drafts: list | None = None,
) -> dict:
    """The console's chair row (holder, state), the lease file, the chair.json record and the edge's reads
    (status line, open action, action-log rows, inbox entries, drafts) as the schema-1 chair. Pure: `at` is
    the one clock.

    `current_action` is the one null the feed allows: no action is running. Day boundaries are Eastern, the
    zone the status line prints in.
    """
    holder = str(row.get("holder") or lease.get("holder") or "")
    # The console row carries the label alone; the lease's holder carries `label@host:pid`.
    full = next((str(h) for h in (row.get("holder"), lease.get("holder")) if h and "@" in str(h)), "")
    host = full.split("@", 1)[1].split(":", 1)[0] if full else ""
    age = beat_age_s(record.get("heartbeat_at"), at)
    last_tick_at, last_status = status
    offset = at.astimezone(EASTERN).utcoffset()
    rows = action_rows or []
    extras = {
        **chair_counters(_as_results(rows, at, offset), rows, last_housekeeping_at, at, offset),
        **chair_attention(inbox_entries, drafts),
    }
    return {
        "holder": holder,
        "host": host,
        "epoch": int(lease.get("epoch") or 0),
        "liveness": str(row.get("state") or "unknown"),
        "beat_age_s": max(0, age) if age is not None else 0,
        # Neither the console row nor chair.lease.json (holder and epoch only) carries `claude_session`;
        # chair.json does, written by `chair.take` and kept by `chair.beat`.
        "session": str(record.get("claude_session") or "")[:8],
        "last_tick_at": last_tick_at or "",
        "last_status": last_status or "",
        "tick_age_s": _tick_age_s(last_tick_at, at),
        "current_action": action,
        "today": chair_today(rows, needs_chair_open, at, offset),
        **{key: int(value or 0) for key, value in extras.items()},
    }


def _as_results(action_rows: list[dict], at: datetime, utc_offset: timedelta) -> list[dict]:
    """Today's action-log rows in the `Result` shape `lands_this_tick` reads; the feed holds no per-tick results."""
    start = local_midnight(at, utc_offset)
    return [
        {"action": {"kind": row.get("kind")}, "status": row.get("status")}
        for row in action_rows
        if (stamp := _ts(row)) is not None and stamp >= start
    ]


def _spend_v1(spend: dict) -> dict:
    """An unknown fraction reads 0.0 and an unknown reset time "": the feed's consumers take no nulls."""
    return {
        key: (value if value is not None else (0.0 if key.endswith("_fraction") else ""))
        for key, value in spend.items()
    }


def _today_v1(runs_dir: Path, now: str) -> dict | None:
    """Edge. `spend.today`: the figures the docket's `efficiency:` line prints, from the same function; None with no store."""
    # Deferred: cli.py imports this module, so a top-level import of cli is circular.
    from agent_tools import cli

    return cli._efficiency_figures(Path(runs_dir), now)


def _machine_v1(row: dict, at: datetime, lanes: Sequence[dict] = ()) -> dict:
    """A hosts-table row (with the console's `in_use`) as the schema-1 machine, with its lane entries and the load its beat carries."""
    versions = row.get("versions_json") or {}
    if isinstance(versions, str):
        try:
            versions = json.loads(versions)
        except ValueError:
            versions = {}
    return {
        "name": str(row.get("name") or ""),
        "state": str(row.get("state") or ""),
        "lanes_in_use": int(row.get("in_use") or 0),
        "capacity": int(row.get("capacity") or 0),
        "login_ok": versions.get("login_ok"),
        "login_checked_at": str(versions.get("login_checked_at") or ""),
        "beat_age_s": _age_s(row.get("beat_at"), at),
        "checkouts": {},
        "lanes": list(lanes),
        "load": dash_machine_lanes.host_load(versions),
    }


def _run_v1(lane: console_screen.LaneRow, local_name: str) -> dict:
    """A live lane as the schema-1 run; a lane with no host of its own runs on this machine."""
    return {
        "run": lane.run,
        "machine": lane.host or local_name,
        "phase": lane.phase or "",
        "node": lane.node or "",
        "attempt": int(lane.attempt or 0),
        "turns": int(lane.turns or 0),
        "cost": float(lane.cost_usd or 0.0),
        "verdict": "",
        "status": "running",
    }


def _finished_status(row: Mapping) -> str:
    """An ended run is approved, landed or quarantined when the store says so, and exited otherwise."""
    return row["status"] if row.get("status") in ("approved", "landed", "quarantined") else "exited"


def _finished_record(row: Mapping, calls: Sequence[Mapping], phases: Sequence[str], events: Sequence) -> dict:
    """A `runs` row with its calls, phases and events as the record `finished_runs` takes. `machine` is empty: the row carries no host."""
    verdicts = [e.detail["verdict"] for e in events if e.kind == "verdict"]
    return {
        "run": row["run_id"],
        "machine": "",
        "phase": phases[-1] if phases else "",
        "node": calls[-1]["node"] if calls else "",
        "attempt": calls[-1]["attempt"] if calls else 0,
        "turns": sum(int(c["turns"] or 0) for c in calls),
        "cost": round(sum(float(c["cost_usd"] or 0.0) for c in calls), 4),
        "verdict": verdicts[-1] if verdicts else "",
        "status": _finished_status(row),
        "ended_at": row["ended_at"],
        # Carried so `gather_feed` reads each run's calls once; `finished_runs` drops keys it does not know.
        "calls": list(calls),
    }


def _finished_records(runs_dir: Path, live_ids: Collection[str]) -> list[dict]:
    """Edge: the newest `LIMIT` ended runs outside `live_ids`, each read the way `runs_top_screen._fact` reads a run."""
    ended = [r for r in exit_rows(runs_dir) if r["ended_at"] is not None and r["run_id"] not in live_ids]
    records = []
    for row in sorted(ended, key=lambda r: str(r["ended_at"]), reverse=True)[:LIMIT]:
        run = str(row["run_id"])
        try:
            lines = (runs_dir / f"{run}.log").read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            lines = []
        calls = _calls_of(run_store.usage(runs_dir, run))
        records.append(
            _finished_record(row, calls, run_store.phase_names(runs_dir, run), events_module.from_log(run, lines))
        )
    return records


def _calls_of(usage: Mapping | None) -> list[dict]:
    """A usage record's calls as `calls_from_usage` rows, each with the stored `task_id` ("" when it has none), which `calls_from_usage` drops. The rows are 1:1 and in order with the stored calls."""
    stored = (usage or {}).get("calls") or []
    return [{**row, "task_id": str(call.get("task_id") or "")} for row, call in zip(calls_from_usage(stored), stored)]


def _run_calls(runs_dir: Path, run: str) -> list[dict]:
    """Edge: a run's calls as `_calls_of` rows. `run_store.usage` reads `<run>.usage.json` beside the pidfile for a local run and the shared store rows for a remote one; `[]` when neither has any."""
    return _calls_of(run_store.usage(runs_dir, run))


def _call_points(calls: Sequence[Mapping]) -> list[dict]:
    """`calls_from_usage` rows as the `{at, cost_usd, node}` calls `run_cost_series` takes; a call with no `ts` is dropped."""
    return [
        {"at": call["ts"], "cost_usd": float(call.get("cost_usd") or 0.0), "node": call["node"]}
        for call in calls
        if call.get("ts")
    ]


def _utc(text) -> datetime | None:
    """An ISO `text` as an aware datetime, a naive one read as UTC; None when it cannot be read."""
    try:
        parsed = _parse_now(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _spend_points(calls_by_run: Mapping[str, Sequence[Mapping]]) -> list[tuple[datetime, float]]:
    """Every run's calls as the `(time, cost_usd)` pairs `spend_series` takes; a call with no readable `ts` is dropped."""
    stamped = [(_utc(call.get("ts")), call) for calls in calls_by_run.values() for call in calls if call.get("ts")]
    return [(ts, float(call.get("cost_usd") or 0.0)) for ts, call in stamped if ts is not None]


def _short_id(slug: str | None, ids: Mapping[str, str]) -> str | None:
    """The short id `ids` holds for an initiative slug; None for no slug or a slug `ids` lacks."""
    return ids.get(slug) if slug else None


def _projects(rows: Sequence[Mapping]) -> dict[str, str | None]:
    """Initiative slug to the basename of its repository; None for a missing or empty repo."""
    def basename(row: Mapping) -> str | None:
        extra = row.get("extra")
        repo = extra.get("repo") if isinstance(extra, Mapping) else None
        return os.path.basename(str(repo).rstrip("/")) or None if repo else None

    return {row["initiative"]: basename(row) for row in rows if row.get("initiative")}


def _project(slug: str | None, projects: Mapping[str, str | None]) -> str | None:
    """The project `projects` holds for an initiative slug; None for no slug or a slug `projects` lacks."""
    return projects.get(slug) if slug else None


def _run_slug(run: str) -> str:
    """The initiative slug of a run key: the key with its trailing `-<digits>` removed."""
    return re.sub(r"-\d+$", "", run)


def _task_slug(target: str, ids: Mapping[str, str]) -> str | None:
    """The longest slug in `ids` that `target` starts with, followed by `-`; None when none does."""
    return max((slug for slug in ids if target.startswith(f"{slug}-")), key=len, default=None)


def _run_number(run: str) -> int:
    """The trailing run number of a run key; -1 when it has none."""
    found = re.search(r"-(\d+)$", run)
    return int(found.group(1)) if found else -1


def _attempts_by_task(attempts: Sequence[Mapping], slug: str) -> dict[str, list[Mapping]]:
    """The attempts of runs of one initiative, by task id."""
    grouped: dict[str, list[Mapping]] = {}
    for attempt in attempts:
        run, task = attempt.get("run_id"), attempt.get("task_id")
        if run and task and _run_slug(str(run)) == slug:
            grouped.setdefault(str(task), []).append(attempt)
    return grouped


def _latest_run(attempts: Sequence[Mapping]) -> str:
    """The run of the highest run number among a task's attempts."""
    return max((str(attempt["run_id"]) for attempt in attempts), key=_run_number)


def _latest_by_task(
    attempts_by_task: Mapping[str, Sequence[Mapping]], calls_by_run: Mapping[str, Sequence[Mapping]],
    scope_of: Callable[[str, str], Mapping | None] | None = None,
) -> dict[str, dict]:
    """Each task's latest run, with the node of its last call there and the turns and cost of its calls there.
    Only calls stamped with the task's id count: a run's calls carry the task they built. `scope_of(task, run)`
    is the task record's `check_scope`; a task it gives none for carries no `check_scope` key."""
    latest = {task: _latest_run(rows) for task, rows in attempts_by_task.items()}
    own = {task: [c for c in calls_by_run.get(run, ()) if c.get("task_id") == task] for task, run in latest.items()}
    return {
        task: {
            "run": run,
            "node": own[task][-1]["node"] if own[task] else "",
            "turns": sum(int(c.get("turns") or 0) for c in own[task]),
            "cost": round(sum(float(c.get("cost_usd") or 0.0) for c in own[task]), 4),
            **({"check_scope": scope} if scope_of and (scope := scope_of(task, run)) is not None else {}),
        }
        for task, run in latest.items()
    }


def _task_check_scope(runs_dir: Path, run: str, phase: str, task: str) -> Mapping | None:
    """Edge: `check_scope` of `<runs_dir>/<run>/tasks/<phase>/<task>.json`; None when the file is missing, unreadable or malformed."""
    try:
        record = json.loads((runs_dir / run / "tasks" / phase / f"{task}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        return None
    scope = record.get("check_scope") if isinstance(record, dict) else None
    return scope if isinstance(scope, dict) else None


def _verdicts_by_task(
    latest: Mapping[str, Mapping], run_verdicts: Mapping[str, tuple[str, frozenset[str]]],
) -> dict[str, str]:
    """A task's verdict is `quarantined` when its latest run's log quarantined it, else that run's last verdict;
    a task whose run left no verdict has none."""
    found = {
        task: ("quarantined" if task in run_verdicts.get(l["run"], ("", frozenset()))[1] else run_verdicts.get(l["run"], ("",))[0])
        for task, l in latest.items()
    }
    return {task: verdict for task, verdict in found.items() if verdict}


def _run_tasks(
    rows: Sequence[Mapping], attempts: Sequence[Mapping], calls_by_run: Mapping[str, Sequence[Mapping]],
    run_verdicts: Mapping[str, tuple[str, frozenset[str]]], slug: str, runs_dir: Path | None = None,
) -> tuple[list[dict], dict]:
    """`(task_entries, counts)` for one initiative's task rows; a row with no task id is left out. With `runs_dir`,
    each task's latest run record supplies its check figures."""
    own = [row for row in _own_tasks(rows, slug) if row.get("task_id")]
    by_task = _attempts_by_task(attempts, slug)
    phase_of = {str(row["task_id"]): str(row.get("phase") or "") for row in own}
    scope_of = (lambda task, run: _task_check_scope(runs_dir, run, phase_of.get(task, ""), task)) if runs_dir else None
    latest = _latest_by_task(by_task, calls_by_run, scope_of)
    entries = task_entries(own, by_task, latest, _verdicts_by_task(latest, run_verdicts))
    land_at = _configured_land_at(rows, slug)
    merged = sum(1 for row in own if _task_state(row, (), False, False, land_at) == "merged")
    return entries, {**counts(entries), "merged": merged}


def _run_verdicts(runs_dir: Path, run: str) -> tuple[str, frozenset[str]]:
    """Edge: a run's last logged verdict ("" when none) and the tasks its log quarantined, read as `_finished_record` reads events."""
    try:
        lines = (runs_dir / f"{run}.log").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        lines = []
    events = events_module.from_log(run, lines)
    verdicts = [e.detail["verdict"] for e in events if e.kind == "verdict"]
    return (verdicts[-1] if verdicts else "", frozenset(e.detail["task"] for e in events if e.kind == "task_quarantined"))


def _tasks_edge(
    runs_dir: Path, rows: Sequence[Mapping], calls_by_run: Mapping[str, Sequence[Mapping]],
) -> dict[str, tuple[list[dict], dict]] | None:
    """Edge: `_run_tasks` for the initiative of every run in `calls_by_run`, keyed by slug. `rows` is the queue read once
    for the history. A queue with any row is a readable queue, and then every run gets an entry: an initiative with no
    task rows gets `[]` and zero counts. `None` only for an empty queue, which cannot be told from an unreadable
    one, and for a store error while the attempts are read; the runs then carry no task keys, so an absent `tasks`
    means "no task data was readable" and never "no tasks". Only store and file errors are caught: a fault in
    the code raises. The attempts are those of every stored run of those initiatives, one query; each latest run's
    log is read once, however many tasks share it."""
    if not rows:
        return None
    slugs = {_run_slug(run) for run in calls_by_run}
    try:
        stored = {str(row["run_id"]) for row in exit_rows(runs_dir) if row.get("run_id")}
        attempts = run_store.attempt_causes_for(
            runs_dir, sorted(run for run in {*stored, *calls_by_run} if _run_slug(run) in slugs),
        )
        latest_runs = {_latest_run(grouped) for slug in slugs for grouped in _attempts_by_task(attempts, slug).values()}
        verdicts = {run: _run_verdicts(runs_dir, run) for run in latest_runs}
        return {slug: _run_tasks(rows, attempts, calls_by_run, verdicts, slug, runs_dir) for slug in slugs}
    except (OSError, *run_store._db_errors()):  # a store failure must not take the feed down; a code fault must
        return None


def _building_tasks(states: Mapping[tuple[str, str, str], str]) -> dict[tuple[str, str], list[str]]:
    """The ids of the tasks in the `building` state, by `(initiative, phase)`."""
    building = [(initiative, phase, task) for (initiative, phase, task), state in states.items() if state == "building"]
    return {(i, p): sorted(t for i2, p2, t in building if (i2, p2) == (i, p)) for i, p, _ in building}


def _started_at(calls_by_run: Mapping[str, Sequence[Mapping]]) -> dict[str, datetime | None]:
    """Each run's earliest call time; None for a run with no readable call `ts`."""
    return {
        run: min((ts for ts in (_utc(call.get("ts")) for call in calls if call.get("ts")) if ts is not None), default=None)
        for run, calls in calls_by_run.items()
    }


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
    """The chair action log's lines as dicts; [] when it is missing, blank lines and malformed ones skipped."""
    try:
        lines = (runs_dir / ACTION_LOG).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    rows = []
    for line in lines:
        try:
            value = json.loads(line) if line.strip() else None
        except ValueError:
            value = None
        if isinstance(value, dict):
            rows.append(value)
    return rows


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
    An unreadable store gives no rows."""
    offset = at.astimezone(EASTERN).utcoffset() or timedelta(0)
    starts = [local_midnight(at, offset), *_window_starts(spend, at)]
    since = min(start.astimezone(UTC) for start in starts).isoformat()
    try:
        return read_history_rows(runs_dir, since)
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


def gather_feed(runs_dir: Path, work_dir: Path, now: str, profile: dict | None = None) -> dict:
    """Edge: the live schema-1 snapshot from the fleet's existing readers, every section mapped to the shape
    tests/fixtures/dash_feed_v1.json fixes (the contract coxtop parses), with no nulls and nothing json can't write,
    except a machine's `login_ok`, which is null while its login is unchecked, a row's `short_id`, which is
    null when its initiative has none, a row's `project`, which is null when its initiative has no known
    repository, and `spend.today`, which is null when the run store is unavailable (its `per_landed` is null
    when nothing landed and its `first_try_rate` when nothing was measured). `spend.today` is a new key
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
    `sessions_priced`. The key is absent when the activity section did not gather sessions."""
    return _build(Path(runs_dir), Path(work_dir), now, profile, lambda name, compute, anchor=None, extra=(): compute())


def _build(
    runs_dir: Path, work_dir: Path, now: str, profile: dict | None, hold,
    cursors: LiveCursors | None = None, on_cursors: Callable[[LiveCursors], None] | None = None,
) -> dict:
    """Edge: the body of `gather_feed`. `hold(name, compute, anchor, extra)` gives one section's value:
    `compute()` uncached, a memoised value in `gather_feed_cached`. `anchor` is read after a compute only and names the
    stamps the section's ages count from; `extra` is the section's time-dependent inputs already read, for its key.
    `cursors` and `on_cursors` make the live block read incrementally; see `_live_edge`."""
    at = _parse_now(now)
    local_name, local_capacity = _local_identity(runs_dir, profile or {})
    local_versions = _local_versions(_local_login(local_name), now)
    spend = _spend(runs_dir, now)
    sections = console_screen.gather(
        runs_dir, work_dir, now, local_name, local_capacity, spend, local_versions=local_versions,
    )
    chair_row = sections["chair"][0] if sections["chair"] else {}
    live_phases = _live_lanes(sections["lanes"])
    blob = _courier_blob(work_dir)
    entries = courier.inbox(blob)
    # A state callable that answers nothing: the feed lists each awaiting PR with its url and never asks the forge.
    review_prs = read_review_prs(runs_dir, lambda url: {})
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
    task_rows = _queue_rows_edge(runs_dir)
    states = _queue_states_edge(runs_dir, live_phases, task_rows)

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
    activity = _activity(
        runs_dir, profile or {}, chair_block["session"], _workspace(work_dir), spend_block, at, wide_rows,
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
        # Read on every refresh, not held: the section fingerprints do not stat schedule.json.
        schedule=_schedule_edge(runs_dir),
    )


@dataclass(frozen=True)
class FeedCache:
    """What one cached refresh hands the next: the section memo, the fingerprints it probed, and the (cpu, wall)
    clock readings at its end."""

    memo: dash_feed_memo.Memo
    fingerprints: Mapping[str, object]
    clock: tuple[float, float]
    live: LiveCursors = LiveCursors()


EMPTY_CACHE = FeedCache(dash_feed_memo.EMPTY, {}, (0.0, 0.0))

# The fingerprint groups each section's builders read. Wrong belief to avoid: "machines reads only the store". Its
# lane entries come from `_board_edge`, so it reads what the board reads. Only `epics` is on the store alone.
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
}
# The finest step, in seconds, a section's output moves with `now` once its age fields are refreshed and its other
# time-dependent inputs are in its key (`extra` in `_build`: the lanes, the hosts, the run window, the console chair
# row). Chair counters and history count from local midnight, which falls on a minute. `spend.today` counts a UTC
# day, which falls on an hour. The rest move with `now` only through those keyed inputs.
_SECTION_STEP_S = {
    "chair": 60, "spend": 3600, "history": 60, "machines": 3600, "runs": 3600,
    "queue": 3600, "inbox": 3600, "board": 3600, "epics": 3600,
    "live": int(min(dash_live_series.MINUTE, dash_live_series.HOUR).total_seconds()),
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

    return tuple(read(path) for path in (work_dir / "courier.jsonl", *sorted(work_dir.glob("*/initiative.md"))))


def _section_key(name: str, fingerprints: Mapping[str, object], at: datetime, work_stamp: tuple) -> tuple:
    """A section's memo key: its groups' fingerprints, `at` floored to its step, and the work-dir stamp where it reads
    the work dir. A group with no fingerprint (a non-sqlite store) gives a key equal to no other."""
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
    fingerprints = dash_feed_inputs.probe(runs_dir, Path(_PROJECTS_DIR).expanduser(), _store_is_local_sqlite(runs_dir))
    stamp = _work_stamp(work_dir)
    state: dict = {"memo": cache.memo, "live": cache.live}
    results: list[tuple[str, bool]] = []

    def hold(name: str, compute, anchor=None, extra=()):
        key = (*_section_key(name, fingerprints, at, stamp), extra)
        value, state["memo"], reused = dash_feed_memo.reuse(
            state["memo"], name, key, lambda: (compute(), None if anchor is None else anchor()),
        )
        results.append((name, reused))
        section, anchors = value
        return _refresh_ages(name, section, anchors, at) if reused else section

    feed = _build(runs_dir, work_dir, now, profile, hold, cache.live, lambda cursors: state.update(live=cursors))
    recomputed, reused = dash_feed_memo.split_reuse(dash_feed_cost.SECTION_ORDER, results)
    cpu_s, wall_s = finish()
    cost = dash_feed_cost.cost_block(cpu_s, wall_s, recomputed, reused)
    clock = (start[0] + cpu_s, start[1] + wall_s)
    return {**feed, "cost": cost}, FeedCache(state["memo"], fingerprints, clock, state["live"])
