"""A run's usage: the `<run_id>.usage.json` file first, the read-only SQLite
store `cox.db` only once that file is gone, and only for an ended run. A run has
ended when its `runs` row has an `ended_at`, or when it has no live pid: a
killed run never stamps `ended_at`. Calls alone do not mean the run is done:
graphs writes each call as it finishes. `usages` lists every run that way, and `run_started`
reads a run's `launched_at`. `phase_manifests` reads a run's
`<run_id>:<phase>.json` files first, then the `manifest_record` on the store's
phase rows, ended run or not: a recorded phase is final. Reads only; never
creates, migrates or writes the store."""

from __future__ import annotations

import copy
import functools
import io
import json
import os
import re
import sqlite3
import time
import zlib
from collections import Counter
from collections.abc import Callable, Collection, Hashable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_tools import analytics_snapshot, epic, store_cursor
from agent_tools.chair_read_cache import Stamp, StampCache
from agent_tools.jsonl_log import parse_log
from agent_tools.lake_config import _LITERAL_SECRETS, _from_env
from agent_tools.store_dialect import connect_readonly_url, is_postgres, placeholder
from agent_tools.store_url import TracesRoot, profile_traces_root, read_provider_profile, resolve_store_url


def db_error_types() -> tuple[type[BaseException], ...]:
    """Store errors to catch; psycopg's join them only when it is installed. Imported here so the module loads lazily."""
    try:
        import psycopg
    except ImportError:
        return (sqlite3.DatabaseError,)
    return (sqlite3.DatabaseError, psycopg.Error)


def __getattr__(name: str) -> tuple[type[BaseException], ...]:
    """Keep `run_store._DB_ERRORS` for other modules and cli.py, computed on first access instead of at import."""
    if name == "_DB_ERRORS":
        return db_error_types()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "LEASE_SNAPSHOT_S", "Lane", "ParquetCheck", "TracesUnavailable", "all_phase_manifests", "artifacts_root_for", "attempt_causes", "attempt_causes_for", "build_counts",
    "call_events", "call_from_row", "connect_readonly", "cost_since", "db_error_types", "efficiency_rows", "gate_call_rows",
    "harness_store_api", "hosts", "json_cell", "last_call_at", "latest_chair_meter", "lease", "lease_name_for", "lease_table_for", "live_lanes", "open_store", "parquet_readable", "phase_manifests", "phase_names", "remote_lanes", "resolve_run_id", "run_ids", "run_spans",
    "run_started", "runs_columns", "sql_for", "sqlite_path", "store_url_for", "store_usages", "summarize", "task_verdict_rows", "traces_root_for", "usage", "usages",
]

if TYPE_CHECKING:
    from types import ModuleType

    from harness import store_api


def harness_store_api() -> ModuleType:
    """`harness.store_api`, imported on first use: importing any `harness` module loads the package's runtime (and the
    postgres driver), which a `cox` command that never opens the store must not pay for (test_lazy_heavy_imports)."""
    from harness import store_api

    return store_api


STORE_FILENAME = "cox.db"
_PHASE_FILE = re.compile(r"^[^:]+:(.+)\.json$")
TRACES_DIRNAME = "traces"


class TracesUnavailable(Exception):
    """The trace store is needed but an optional package (`zstandard`, or `pyarrow` for Parquet traces) is not installed."""


# Columns that carry over unchanged from a node_calls row to a usage-file call.
_SAME = (
    "role", "task_id", "tier", "cost_usd", "ceiling_usd", "ceiling_source", "turns", "duration_ms",
    "input_tokens", "cache_read_tokens", "cache_creation_tokens", "input_total", "output_tokens", "ts",
)


def summarize(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Pure: totals and a per-model breakdown over the recorded calls."""
    fields = ("input_tokens", "cache_read_tokens", "cache_creation_tokens", "output_tokens")

    def total(call: Mapping[str, Any]) -> int:
        # Older records carried only a summed `input_tokens`; newer ones split it.
        return int(call.get("input_total") if call.get("input_total") is not None else call.get("input_tokens") or 0)

    by_model: dict[str, dict[str, Any]] = {}
    for call in calls:
        row = by_model.setdefault(str(call.get("model")), {"calls": 0, "cost_usd": 0.0, "input_total": 0, **dict.fromkeys(fields, 0)})
        row["calls"] += 1
        row["cost_usd"] = round(row["cost_usd"] + float(call.get("cost_usd") or 0.0), 4)
        row["input_total"] += total(call)
        for f in fields:
            row[f] += int(call.get(f) or 0)
    return {
        "calls": len(calls),
        "cost_usd": round(sum(float(c.get("cost_usd") or 0.0) for c in calls), 4),
        "turns": sum(int(c.get("turns") or 0) for c in calls),
        "input_total": sum(total(c) for c in calls),
        **{f: sum(int(c.get(f) or 0) for c in calls) for f in fields},
        "by_model": by_model,
    }


def json_cell(raw: Any) -> Any:
    """A store JSON cell decoded: SQLite hands back text, Postgres a decoded value. None when the text is not JSON."""
    if isinstance(raw, (str, bytes)):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return raw


def _detail_of(row: Mapping[str, Any]) -> dict[str, Any]:
    """The row's `detail_json` as an object; empty when the column is absent, null, unparseable or not an object."""
    raw = row["detail_json"] if "detail_json" in row.keys() else None  # noqa: SIM118 -- sqlite3.Row's `in` tests values, not keys
    detail = json_cell(raw)
    return detail if isinstance(detail, dict) else {}


def call_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """One `node_calls` row as a usage-file call. `summary` and `commands_run` ride along from `detail_json` when it holds them."""
    detail = _detail_of(row)
    return {
        **{k: row[k] for k in _SAME},
        "id": row["call_id"],
        "model": row["model_alias"],
        "model_id": row["model_id"] if "model_id" in row.keys() else None,  # noqa: SIM118 -- a store from before the column has none; sqlite3.Row's `in` tests values
        "ok": bool(row["ok"]),
        "decision": json_cell(row["decision_json"]),
        **{k: detail[k] for k in ("summary", "commands_run") if k in detail},
    }


# mirrors cli.DEFAULT_PROFILE; cli imports this module, so it cannot be imported back
_DEFAULT_PROFILE = "~/.config/agent-tools/profile.yaml"


def sql_for(template: str, token: str) -> str:
    """Pure: `template` with each `{p}` marker replaced by the dialect's bind-parameter token."""
    return template.replace("{p}", token)


def sqlite_path(url: str) -> Path:
    return Path(url.removeprefix("sqlite:///") if url.startswith("sqlite:") else url)


def store_url_for(runs_dir: Path) -> str:
    """Edge. The store URL: the provider profile named by the routing profile (`$AGENT_TOOLS_PROFILE` or the default), else `cox.db` in `runs_dir`."""
    return _store_url_for(str(runs_dir), os.environ.get("AGENT_TOOLS_PROFILE") or _DEFAULT_PROFILE)


@functools.lru_cache(maxsize=32)
def _store_url_for(runs_dir: str, routing_profile: str) -> str:
    """Cached per process: liveness asks once per pidfile, and two YAML reads each time made `route context` slow."""
    routing = read_provider_profile(routing_profile)
    named = routing.get("provider_profile")
    return resolve_store_url(named if isinstance(named, str) else "", runs_dir)


def traces_root_for(runs_dir: Path) -> TracesRoot:
    """Edge. The traces root: the `traces_url` of the provider profile the routing profile names, else `<runs_dir>/traces`."""
    return _traces_root_for(str(runs_dir), os.environ.get("AGENT_TOOLS_PROFILE") or _DEFAULT_PROFILE)


@functools.lru_cache(maxsize=32)
def _traces_root_for(runs_dir: str, routing_profile: str) -> TracesRoot:
    """Cached per process, like `_store_url_for`: a call's events are asked for once per call."""
    named = read_provider_profile(routing_profile).get("provider_profile")
    return profile_traces_root(read_provider_profile(named) if isinstance(named, str) and named else {}, runs_dir)


def artifacts_root_for(runs_dir: Path) -> Any:
    """Edge. The step artifacts root (`harness.artifacts.ArtifactsRoot`): the `artifacts_url` of the provider profile the
    routing profile names, resolved as the save path resolves it; None when the profile sets none. A bad URL raises
    ValueError. Not cached: the root holds the environment's secret values, read now."""
    from harness.artifacts import artifacts_root  # the harness loads on use, as `harness_store_api` does

    named = read_provider_profile(os.environ.get("AGENT_TOOLS_PROFILE") or _DEFAULT_PROFILE).get("provider_profile")
    return artifacts_root(read_provider_profile(named) if isinstance(named, str) and named else {}, os.environ, runs_dir)


def open_store(runs_dir: Path) -> tuple[Any, str] | None:
    """Edge. A read-only connection and its bind-parameter token; None when a SQLite store file is absent."""
    url = store_url_for(Path(runs_dir))
    if not is_postgres(url) and not sqlite_path(url).exists():
        return None
    return connect_readonly_url(url), placeholder(url)


def connect_readonly(runs_dir: Path) -> Any | None:
    """None when a SQLite store is absent. Opened read-only, so it can never create the file. Rows are addressable by column name."""
    opened = open_store(runs_dir)
    return None if opened is None else opened[0]


def _change_stamp_sql() -> str:
    """One row per user table the connection's `search_path` resolves to: its name and the rows inserted, updated and
    deleted since statistics began. `pg_table_is_visible` keeps a same-named table in another schema (a `public.work_items`
    beside the store's own, say) out, so its writes neither move the stamp nor hide the store's."""
    return (
        "SELECT relname, n_tup_ins + n_tup_upd + n_tup_del AS changes FROM pg_stat_user_tables "
        "WHERE pg_table_is_visible(relid)"
    )


def _stamp_of_rows(rows: Sequence[Any]) -> Hashable:
    """Sorted (table, changes) pairs, so row order never moves the stamp. Accepts mapping rows and plain pairs."""
    pairs = ((r["relname"], int(r["changes"])) if isinstance(r, Mapping) else (r[0], int(r[1])) for r in rows)
    return tuple(sorted(pairs))


def table_counters(runs_dir: Path) -> dict[str, int] | None:
    """Edge. A counter per user table, for the chair's reader cache: each table's inserted, updated and deleted row
    count (`_change_stamp_sql`), with each table the store's change cursor watches (`store_cursor`, schema 21) read
    from the cursor instead, in a second query on the same connection only when the cursor table is visible. A
    watched table's counter moves when its write commits; any other table's trails a write as `store_change_stamp`
    says. None for a SQLite store, an absent store or any failure, which the cache reads as "re-read everything".
    Never raises."""
    try:
        if not is_postgres(store_url_for(Path(runs_dir))):
            return None
        opened = open_store(runs_dir)
        if opened is None:
            return None
        conn = opened[0]
        try:
            statistics = dict(_stamp_of_rows(conn.execute(_change_stamp_sql()).fetchall()))  # type: ignore[arg-type]
            present = store_cursor.TABLE in statistics
            cursor = store_cursor.seqs(conn.execute(store_cursor.READ_SQL).fetchall()) if present else None
            return store_cursor.merged(statistics, cursor)
        finally:
            conn.close()
    except Exception:  # never raise: as `store_change_stamp`, any failure means "always changed"
        return None


def store_change_stamp(runs_dir: Path) -> Hashable | None:
    """Edge. A value that moves when any table the feed reads is written; None means treat the store as always changed.

    The feed reads runs, leases, hosts, node_calls, attempts, phases, task_records, work_items, chair_actions and
    export_hashes. The stamp sums inserted, updated and deleted rows for every user table, so it covers those and
    any table added later. Reads leave the counters alone, which a transaction commit counter would not.
    The counters are cumulative statistics, not part of the write's transaction. The writing backend flushes them
    when it goes idle, no more often than about once a second and, under lock contention, as late as about 60 s.
    So the stamp can trail a committed write by about 10 s, and up to about 60 s on a busy server. A section held
    over that gap shows its pre-write value until the stamp moves or its own step runs out.
    The stamp never moves when the counters do not count this database's writes: on a read replica (a standby keeps
    no write counters of its own) or with `track_counts = off`. Then each store-driven section goes stale for up to
    its hold step in `dash_feed._SECTION_STEP_S`, an hour for most. Point the feed at the primary, with
    `track_counts` on.
    TRUNCATE is not counted as deleted rows, and the counters restart on crash recovery or `pg_stat_reset`.
    None for a SQLite store, an absent store or any failure, from config, psycopg or the database. Never raises.
    """
    try:
        if not is_postgres(store_url_for(Path(runs_dir))):
            return None
        opened = open_store(runs_dir)
        if opened is None:
            return None
        conn = opened[0]
        try:
            return _stamp_of_rows(conn.execute(_change_stamp_sql()).fetchall())
        finally:
            conn.close()
    except Exception:  # never raise: store config, a missing psycopg and an unreachable Postgres all mean "always changed"
        return None


def run_started_at(runs_dir: Path, run_id: str) -> str | None:
    """Edge. `runs.started_at` text for `run_id`; None with no store, no row, a null value or an unreadable store."""
    opened = open_store(runs_dir)
    if opened is None:
        return None
    conn, token = opened
    try:
        row = conn.execute(sql_for("SELECT started_at FROM runs WHERE run_id = {p}", token), (run_id,)).fetchone()
    except db_error_types():
        return None
    finally:
        conn.close()
    return None if row is None or row["started_at"] is None else str(row["started_at"])


def lease_name_for(run_id: str) -> str:
    # mirrors graphs `harness/run_lease.lease_name`: the lease is per prefix, so `x-3` and `x-4` share `runs:x`
    return "runs:" + re.sub(r"-\d+$", "", run_id)


def lease(runs_dir: Path, run_id: str) -> tuple[str, str, str] | None:
    """Edge. The (holder, expires_at, heartbeat_at) of the store lease for `run_id`'s prefix; None with no store, no row, or an unreadable store."""
    return lease_table_for(str(runs_dir), _snapshot_window()).get(lease_name_for(run_id))


LEASE_SNAPSHOT_S = 2  # one read of the leases table serves every liveness check within this window
_snapshot_clock: Callable[[], float] = time.monotonic  # the windows' clock; a test swaps in one it moves by hand


def _snapshot_window() -> int:
    """Edge. The `LEASE_SNAPSHOT_S` window the clock is in now. Windows are aligned buckets, so two reads a moment apart
    fall in two windows when a boundary passes between them, and each window reads the table once."""
    return int(_snapshot_clock() // LEASE_SNAPSHOT_S)


@functools.lru_cache(maxsize=8)
def lease_table_for(runs_dir: str, _window: int) -> dict[str, tuple[str, str, str]]:
    """Every lease row by name, read once per `LEASE_SNAPSHOT_S` window: a docket asks about ~800 pidfiles."""
    opened = open_store(Path(runs_dir))
    if opened is None:
        return {}
    conn, _ = opened
    try:
        rows = conn.execute("SELECT name, holder, expires_at, heartbeat_at FROM leases").fetchall()
    except db_error_types():
        return {}
    finally:
        conn.close()
    return {r["name"]: (r["holder"], r["expires_at"], r["heartbeat_at"]) for r in rows}


@dataclass(frozen=True)
class LeaseRow:
    """A store lease row. `name` is the lease name (`runs:x`). `status` and `progress` are None when the leases table lacks the column."""

    name: str
    holder: str
    epoch: int
    expires_at: str
    heartbeat_at: str
    status: str | None
    progress: str | None


def _leases_columns(conn: Any, token: str) -> set[str]:
    """Edge. The column names of `leases`, asked of the backend: a failed SELECT would abort a Postgres transaction."""
    if token == placeholder("postgres://"):
        sql = "SELECT column_name AS name FROM information_schema.columns WHERE table_name = {p} AND table_schema = current_schema()"
        return {r["name"] for r in conn.execute(sql_for(sql, token), ("leases",)).fetchall()}
    return {r["name"] for r in conn.execute("PRAGMA table_info(leases)").fetchall()}


def lease_row(runs_dir: Path, run_id: str) -> LeaseRow | None:
    """Edge. The full store lease row for `run_id`'s prefix; None with no store, no row, or an unreadable store."""
    return _lease_rows(str(runs_dir), _snapshot_window()).get(lease_name_for(run_id))


@functools.lru_cache(maxsize=8)
def _lease_rows(runs_dir: str, _window: int) -> dict[str, LeaseRow]:
    """Every lease row by name with its epoch, read once per `LEASE_SNAPSHOT_S` window. `status` and `progress` are read only when the table has them."""
    opened = open_store(Path(runs_dir))
    if opened is None:
        return {}
    conn, token = opened
    try:
        columns = _leases_columns(conn, token)
        extra = ", ".join(c if c in columns else f"NULL AS {c}" for c in ("status", "progress"))
        rows = conn.execute(f"SELECT name, holder, epoch, expires_at, heartbeat_at, {extra} FROM leases").fetchall()
    except db_error_types():
        return {}
    finally:
        conn.close()
    return {
        r["name"]: LeaseRow(r["name"], r["holder"], r["epoch"], r["expires_at"], r["heartbeat_at"], r["status"], r["progress"])
        for r in rows
    }


@dataclass(frozen=True)
class Lane:
    """A live lease joined to its newest run row. `host` is None when the store's `runs` table has no `host` column."""

    run: str
    host: str | None
    launched_at: str
    heartbeat_at: str
    phases: int = 1


def _progress_nodes(progress: str | None) -> list[Any]:
    try:
        body = json.loads(progress) if progress else None
    except ValueError:
        return []
    nodes = body.get("nodes") if isinstance(body, dict) else None
    return nodes if isinstance(nodes, list) else []


def running_phases(progress: str | None) -> int:
    """Distinct non-empty `phase` values among the `running` nodes of a lease's progress JSON; 1 when there are none or the text does not parse."""
    phases = {
        n["phase"] for n in _progress_nodes(progress) if isinstance(n, dict) and n.get("status") == "running" and isinstance(n.get("phase"), str) and n["phase"]
    }
    return max(1, len(phases))


def runs_columns(conn: Any, token: str) -> set[str]:
    """Edge. The column names of `runs`, asked of the backend: a failed SELECT would abort a Postgres transaction."""
    if token == placeholder("postgres://"):
        sql = "SELECT column_name AS name FROM information_schema.columns WHERE table_name = {p} AND table_schema = current_schema()"
        return {r["name"] for r in conn.execute(sql_for(sql, token), ("runs",)).fetchall()}
    return {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}


def _newest_run(conn: Any, token: str, name: str, host_expr: str) -> Any | None:
    """Edge. The run row of lease `name`'s prefix with the latest `launched_at`; None when the prefix has no run."""
    prefix = name.removeprefix("runs:")
    like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "-%"
    sql = sql_for(
        f"SELECT run_id, launched_at, {host_expr} FROM runs WHERE (run_id = {{p}} OR run_id LIKE {{p}} ESCAPE '\\') ORDER BY launched_at DESC",
        token,
    )
    return next((r for r in conn.execute(sql, (prefix, like)).fetchall() if lease_name_for(r["run_id"]) == name), None)


def live_lanes(runs_dir: Path, now: str) -> list[Lane]:
    """Edge. Every `runs:` lease with `expires_at` later than `now` (ISO UTC, passed in), joined to its prefix's newest run row, by lease name.

    A lease with no run row is skipped. `phases` is `running_phases` of the lease's progress, read in the same snapshot.
    Empty with no store or an unreadable one."""
    rows = _lease_rows(str(runs_dir), _snapshot_window())
    live = sorted((n, r.heartbeat_at, running_phases(r.progress)) for n, r in rows.items() if n.startswith("runs:") and r.expires_at > now)
    return _joined_lanes(runs_dir, live)


def lost_lanes(runs_dir: Path, now: str) -> list[Lane]:
    """Edge. Every `runs:` lease that lapsed without a release: `expires_at` is at or before `now` but later than its `heartbeat_at`.

    A release writes a 1970 `expires_at`, so a released lease is never listed. A lane whose host died stops renewing, and
    its lease lapses a ttl after its last beat, so it is listed here and not in `live_lanes`."""
    table = lease_table_for(str(runs_dir), _snapshot_window())
    lapsed = sorted((n, beat, 1) for n, (_, expires, beat) in table.items() if n.startswith("runs:") and beat < expires <= now)
    return _joined_lanes(runs_dir, lapsed)


def _joined_lanes(runs_dir: Path, live: list[tuple[str, str, int]]) -> list[Lane]:
    """Edge. Each (lease name, heartbeat, phases) joined to its prefix's newest run row; a lease with no run row is skipped."""
    opened = open_store(runs_dir) if live else None
    if opened is None:
        return []
    conn, token = opened
    try:
        host_expr = "host" if "host" in runs_columns(conn, token) else "NULL AS host"
        joined = [(_newest_run(conn, token, name, host_expr), beat, phases) for name, beat, phases in live]
    except db_error_types():
        return []
    finally:
        conn.close()
    return [Lane(r["run_id"], r["host"], r["launched_at"], beat, phases) for r, beat, phases in joined if r is not None]


def newest_run_hosts(runs_dir: Path, initiatives: Collection[str]) -> dict[str, str]:
    """Edge. Each initiative's newest run's `runs.host` ("" when the column is absent or the row's host is blank).

    An initiative with no run row is omitted. Empty with no store or an unreadable one."""
    opened = open_store(runs_dir)
    if opened is None:
        return {}
    conn, token = opened
    try:
        host_expr = "host" if "host" in runs_columns(conn, token) else "NULL AS host"
        rows = {i: _newest_run(conn, token, f"runs:{i}", host_expr) for i in initiatives}
    except db_error_types():
        return {}
    finally:
        conn.close()
    return {i: (r["host"] or "") for i, r in rows.items() if r is not None}


def hosts(runs_dir: Path) -> list[dict[str, object]]:
    """Edge. The `hosts` table's rows by name; empty with no store, no table, or an unreadable store. Read every chair tick, so it never raises."""
    try:
        opened = open_store(runs_dir)
    except (*db_error_types(), RuntimeError):  # an unreachable Postgres, or psycopg not installed
        return []
    if opened is None:
        return []
    conn, _ = opened
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM hosts ORDER BY name").fetchall()]
    except db_error_types():
        return []
    finally:
        conn.close()


def remote_lanes(lanes: Sequence[Lane], local_runs: Collection[str]) -> list[Lane]:
    """Pure: the lanes whose run no local pidfile names, in input order. A run with a pidfile is local even when its lease is live."""
    return [lane for lane in lanes if lane.run not in local_runs]


def run_ids(runs_dir: Path) -> set[str]:
    """Edge. Every `run_id` in the store's `runs` table; empty with no store or an unreadable one."""
    opened = open_store(runs_dir)
    if opened is None:
        return set()
    conn, _ = opened
    try:
        return {row["run_id"] for row in conn.execute("SELECT run_id FROM runs")}
    except db_error_types():
        return set()
    finally:
        conn.close()


def task_run_phases(runs_dir: Path, initiative: str, task_id: str) -> list[tuple[str, str]]:
    """Edge. `(run_id, phase_id)` of every record of `task_id` in a run of `initiative` (id `<initiative>-N`), newest run first.
    Empty with no store or an unreadable one."""
    sql = (
        "SELECT t.run_id AS run_id, t.phase_id AS phase_id FROM task_records t LEFT JOIN runs r ON r.run_id = t.run_id"
        " WHERE t.task_id = {p} ORDER BY r.started_at DESC, t.run_id DESC"
    )
    rows = _store_tuples(runs_dir, sql, (task_id,), ("run_id", "phase_id"))
    return [(str(run), str(phase)) for run, phase in rows if str(run).rsplit("-", 1)[0] == initiative]


_GATE_FACTS = ("handoff_verdict", "charter_verdict", "adversary_verdict", "arbiter_verdict", "fix_loop_attempts",
               "fix_loop_stopped", "plan_gate_verdict")


def _task_verdict_row(record: Mapping[str, Any], run_id: str, task_id: str) -> dict[str, Any]:
    """Pure: one task record as a `stats_gates` task row, through the `gate_facts` the stats.db ingest uses."""
    # stats_ingest imports run_store at module level, so this import waits until call time to avoid the cycle.
    from agent_tools.stats_ingest import gate_facts

    facts = gate_facts(record)
    plan_gate = record.get("plan_gate")
    ran = isinstance(plan_gate, Mapping) and plan_gate.get("ran") is True
    return {
        "run_id": run_id,
        "task_id": task_id,
        **{key: facts[key] for key in _GATE_FACTS},
        "plan_gate_verdict": facts["plan_gate_verdict"] or ("pass" if ran else None),
        "outcome": "landed" if record.get("landed") is True else None,
    }


def _one_per_key(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Pure: one row per (run_id, task_id); a key held by records in two phases states no verdict, never one of the two."""
    counts = Counter((row["run_id"], row["task_id"]) for row in rows)
    silent = dict.fromkeys((*_GATE_FACTS, "outcome"))
    return [
        dict(row) if counts[key] == 1 else {"run_id": key[0], "task_id": key[1], **silent}
        for key, row in {(r["run_id"], r["task_id"]): r for r in rows}.items()
    ]


def _record_of(raw: Any) -> Mapping[str, Any] | None:
    """Pure: a `record_json` cell as a dict, from SQLite text or a decoded Postgres value; None when it is not a JSON object."""
    if isinstance(raw, str):
        try:
            return _record_of(json.loads(raw))
        except ValueError:
            return None
    return raw if isinstance(raw, Mapping) else None


# node_calls.task_id is the bare task id, shared by every run that retries it, while task_records key on
# (run_id, phase_id, task_id). Joining on task_id alone would lend a retried task's verdicts to every run's calls,
# so both row kinds carry run_id and `stats_gates.gate_rows` joins on (run_id, task_id). The gate rows carry no
# phase, so records of one task id in two phases of one run collapse to a row with no verdict (`_one_per_key`).
# `since` is one rule for both: a call counts when its ts is at or after it, and a task record counts when its run
# has such a call. task_records has no timestamp, and a run launched before `since` can still call after it.
_SINCE_CALLS = " WHERE ts >= {p}"
_SINCE_TASKS = " WHERE run_id IN (SELECT run_id FROM node_calls WHERE ts >= {p})"


def _store_tuples(runs_dir: Path, template: str, params: Sequence[Any], keys: Sequence[str]) -> list[tuple]:
    """Edge. The `keys` columns of `template` run on the store, as tuples; empty with no store or an unreadable one."""
    opened = open_store(runs_dir)
    if opened is None:
        return []
    conn, p = opened
    try:
        return [tuple(r[k] for k in keys) for r in conn.execute(sql_for(template, p), tuple(params)).fetchall()]
    except db_error_types():
        return []
    finally:
        conn.close()


def _snapshot_rows(runs_dir: Path, template: str, params: Sequence[Any], keys: Sequence[str]) -> list[tuple]:
    """Edge. Tuples of the `keys` columns of `template`: from the analytics snapshot on a Postgres store, else straight from the store.

    The snapshot path falls back to the store query when DuckDB or the snapshot is unavailable. A SQLite store never
    touches the snapshot, so it never warns."""

    def fallback() -> list[tuple]:
        return _store_tuples(runs_dir, template, params, keys)

    url = store_url_for(Path(runs_dir))
    if not is_postgres(url):
        return fallback()
    profile = read_provider_profile(os.environ.get("AGENT_TOOLS_PROFILE") or _DEFAULT_PROFILE)
    return analytics_snapshot.query(
        sql_for(template, "?"),  # DuckDB binds with `?`, whatever the store's own token is
        params,
        runs_dir=runs_dir,
        store_url=url,
        now=time.time(),
        max_age_s=analytics_snapshot.max_age_s(profile),
        fallback=fallback,
    )


def task_verdict_rows(runs_dir: Path, since: str | None) -> list[dict[str, Any]]:
    """Edge. One `stats_gates` task row per (run_id, task_id) in `task_records`; empty with no store or an unreadable one."""
    where, params = ("", ()) if since is None else (_SINCE_TASKS, (since,))
    rows = _snapshot_rows(runs_dir, "SELECT run_id, task_id, record_json FROM task_records" + where, params, ("run_id", "task_id", "record_json"))
    records = [(run_id, task_id, _record_of(raw)) for run_id, task_id, raw in rows]
    return _one_per_key([_task_verdict_row(record, run_id, task_id) for run_id, task_id, record in records if record is not None])


def gate_call_rows(runs_dir: Path, since: str | None) -> list[dict[str, Any]]:
    """Edge. `stats_gates` call rows (role, run_id, task_id, cost_usd), `since` as above; empty with no store or an unreadable one."""
    keys = ("role", "run_id", "task_id", "cost_usd")
    where, params = ("", ()) if since is None else (_SINCE_CALLS, (since,))
    return [dict(zip(keys, row, strict=True)) for row in _snapshot_rows(runs_dir, "SELECT role, run_id, task_id, cost_usd FROM node_calls" + where, params, keys)]


def cost_since(runs_dir: Path, since: str, until: str | None = None) -> float | None:
    """Edge. Sum of `node_calls.cost_usd` with `since <= ts < until` (ISO text); None with no store or an unreadable one."""
    opened = open_store(runs_dir)
    if opened is None:
        return None
    conn, p = opened
    bound, params = (" AND ts < {p}", (since, until)) if until is not None else ("", (since,))
    try:
        row = conn.execute(sql_for("SELECT SUM(cost_usd) AS total FROM node_calls WHERE ts >= {p}" + bound, p), params).fetchone()
        return float(row["total"] or 0.0)
    except db_error_types():
        return None
    finally:
        conn.close()


def latest_chair_meter(runs_dir: Path) -> dict | None:
    """Edge. The newest `meter` row's `action_json` as a dict; None for no store, no row, a store error, or a non-object."""
    try:
        opened = open_store(runs_dir)
    except (*db_error_types(), RuntimeError):  # an unreachable Postgres, or psycopg not installed
        return None
    if opened is None:
        return None
    conn, token = opened
    try:
        sql = sql_for("SELECT action_json FROM chair_actions WHERE kind = {p} ORDER BY ts DESC LIMIT 1", token)
        row = conn.execute(sql, ("meter",)).fetchone()
    except db_error_types():
        return None
    finally:
        conn.close()
    # Postgres rows are `dict_row`, so the cell is read by name, never `row[0]`; `json_cell` takes text or a decoded dict.
    doc = json_cell(row["action_json"]) if row is not None else None
    return doc if isinstance(doc, dict) else None


def latest_chair_status(runs_dir: Path) -> dict | None:
    """Edge. The newest `status` row's `action_json` as a dict; None for no store, no row, a store error, or a non-object."""
    try:
        opened = open_store(runs_dir)
    except (*db_error_types(), RuntimeError):  # an unreachable Postgres, or psycopg not installed
        return None
    if opened is None:
        return None
    conn, token = opened
    try:
        sql = sql_for("SELECT action_json FROM chair_actions WHERE kind = {p} ORDER BY ts DESC LIMIT 1", token)
        row = conn.execute(sql, ("status",)).fetchone()
    except db_error_types():
        return None
    finally:
        conn.close()
    doc = json_cell(row["action_json"]) if row is not None else None
    return doc if isinstance(doc, dict) else None


def last_call_at(runs_dir: Path, run_ids: Sequence[str]) -> dict[str, str]:
    """Edge. Each id in `run_ids` mapped to its newest `node_calls.ts`; a run with no call row is absent.
    `{}` with no store, an unreadable one, or an empty `run_ids`."""
    opened = open_store(runs_dir) if run_ids else None
    if opened is None:
        return {}
    conn, p = opened
    try:
        marks = ", ".join(["{p}"] * len(run_ids))
        sql = sql_for(f"SELECT run_id, MAX(ts) AS last FROM node_calls WHERE run_id IN ({marks}) GROUP BY run_id", p)
        return {r["run_id"]: r["last"] for r in conn.execute(sql, tuple(run_ids)).fetchall()}
    except db_error_types():
        return {}
    finally:
        conn.close()


def build_counts(runs_dir: Path, task_ids: Collection[str], runs: Collection[str] = ()) -> dict[str, int]:
    """Edge. Build calls per `task_id` equal to one of `task_ids` or a `<run>:...` composite of one of `runs`; empty with no store or an unreadable one."""
    ids, prefixes = sorted(set(task_ids)), sorted(set(runs))
    opened = open_store(runs_dir) if ids or prefixes else None
    if opened is None:
        return {}
    conn, p = opened
    match = " OR ".join(["task_id = {p}"] * len(ids) + ["task_id LIKE {p} ESCAPE '\\'"] * len(prefixes))
    likes = tuple(r.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ":%" for r in prefixes)
    try:
        sql = sql_for(f"SELECT task_id, COUNT(*) AS n FROM node_calls WHERE role = 'build' AND ({match}) GROUP BY task_id", p)
        return {r["task_id"]: int(r["n"]) for r in conn.execute(sql, (*ids, *likes)).fetchall()}
    except db_error_types():
        return {}
    finally:
        conn.close()


# node_calls.task_id is the bare work-item id, shared by every run that retries it, so a task is its task_id alone
_EFFICIENCY_CALLS = (
    "SELECT substr(ts, 1, 10) AS day, task_id, COALESCE(SUM(cost_usd), 0) AS cost_usd, COALESCE(SUM(turns), 0) AS turns, "
    "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, COALESCE(SUM(input_total), 0) AS input_total "
    "FROM node_calls WHERE ts >= {p} GROUP BY substr(ts, 1, 10), task_id"
)
_EFFICIENCY_TASKS = (
    "SELECT task_id, SUM(CASE WHEN role = 'build' THEN 1 ELSE 0 END) AS builds, MAX(ts) AS last_ts "
    "FROM node_calls WHERE task_id IS NOT NULL GROUP BY task_id"
)


def efficiency_rows(runs_dir: Path, since: str) -> dict[str, list[dict[str, Any]]]:
    """Edge. `calls` per (UTC day, task_id) from `since` on; `tasks` per task_id over all runs and time; empty with no store."""
    call_keys = ("day", "task_id", "cost_usd", "turns", "cache_read_tokens", "input_total")
    task_keys = ("task_id", "builds", "last_ts")
    calls = _snapshot_rows(runs_dir, _EFFICIENCY_CALLS, (since,), call_keys)
    tasks = _snapshot_rows(runs_dir, _EFFICIENCY_TASKS, (), task_keys)
    return {
        "calls": [dict(zip(call_keys, row, strict=True)) for row in calls],
        "tasks": [dict(zip(task_keys, row, strict=True)) for row in tasks],
    }


def _attempts_columns(conn: Any, token: str) -> set[str]:
    """Edge. The column names of `attempts`, asked of the backend: a failed SELECT would abort a Postgres transaction."""
    if token == placeholder("postgres://"):
        sql = "SELECT column_name AS name FROM information_schema.columns WHERE table_name = {p} AND table_schema = current_schema()"
        return {r["name"] for r in conn.execute(sql_for(sql, token), ("attempts",)).fetchall()}
    return {r["name"] for r in conn.execute("PRAGMA table_info(attempts)").fetchall()}


def attempt_causes(runs_dir: Path, since: str) -> list[dict[str, Any]]:
    """Edge. Each attempt's kind, cause, cause_why, reason and ts at or after `since`, oldest first; empty with no store or an unreadable one.

    `ts` is ISO text, so a `YYYY-MM-DD` `since` compares correctly as text. A store below schema 5 has no `cause`
    or `cause_why` column: both read as None."""
    opened = open_store(runs_dir)
    if opened is None:
        return []
    conn, p = opened
    try:
        cols = "cause, cause_why" if "cause" in _attempts_columns(conn, p) else "NULL AS cause, NULL AS cause_why"
        sql = sql_for(f"SELECT kind, {cols}, reason, ts FROM attempts WHERE ts >= {{p}} ORDER BY ts", p)
        keys = ("kind", "cause", "cause_why", "reason", "ts")
        return [{k: r[k] for k in keys} for r in conn.execute(sql, (since,)).fetchall()]
    except db_error_types():
        return []
    finally:
        conn.close()


def attempt_causes_for(runs_dir: Path, run_ids: Sequence[str]) -> list[dict[str, Any]]:
    """Edge. Each attempt's run_id, task_id, seq and cause for the given runs, by run then seq; empty with no store, no runs or an unreadable store.

    A store below schema 5 has no `cause` column: it reads as None."""
    opened = open_store(runs_dir) if run_ids else None
    if opened is None:
        return []
    conn, p = opened
    try:
        cols = "cause" if "cause" in _attempts_columns(conn, p) else "NULL AS cause"
        marks = ", ".join(["{p}"] * len(run_ids))
        sql = sql_for(f"SELECT run_id, task_id, seq, {cols} FROM attempts WHERE run_id IN ({marks}) ORDER BY run_id, seq", p)
        keys = ("run_id", "task_id", "seq", "cause")
        return [{k: r[k] for k in keys} for r in conn.execute(sql, tuple(run_ids)).fetchall()]
    except db_error_types():
        return []
    finally:
        conn.close()


def rescue_failures(runs_dir: Path) -> list[dict[str, Any]]:
    """Edge. Each attempts row of kind rescue_failed as run_id, task_id, phase_id, ts and cause, oldest first; empty with no store or an unreadable one.

    A store below schema 5 has no `cause` column: it reads as None."""
    opened = open_store(runs_dir)
    if opened is None:
        return []
    conn, p = opened
    try:
        cols = "cause" if "cause" in _attempts_columns(conn, p) else "NULL AS cause"
        sql = sql_for(f"SELECT run_id, task_id, phase_id, ts, {cols} FROM attempts WHERE kind = {{p}} ORDER BY ts", p)
        keys = ("run_id", "task_id", "phase_id", "ts", "cause")
        return [{k: r[k] for k in keys} for r in conn.execute(sql, ("rescue_failed",)).fetchall()]
    except db_error_types():
        return []
    finally:
        conn.close()


def run_spans(runs_dir: Path, since: str) -> list[tuple[str, str, str | None]]:
    """Edge. (run_id, launched_at, ended_at) of runs still open or ended at or after `since`, by launch time; empty with no store or an unreadable one.

    A run with no `ended_at` that is not live (killed, or from before ended_at was stamped) ends at its last
    recorded call, or at its launch when it has none; only a live run stays open."""
    opened = open_store(runs_dir)
    if opened is None:
        return []
    conn, p = opened

    def last_call(run_id: str) -> str | None:
        try:
            return conn.execute(sql_for("SELECT MAX(ts) AS last FROM node_calls WHERE run_id = {p}", p), (run_id,)).fetchone()["last"]
        except db_error_types():
            return None

    spans = []
    try:
        rows = conn.execute(
            sql_for(
                "SELECT run_id, launched_at, ended_at FROM runs "
                "WHERE launched_at IS NOT NULL AND (ended_at IS NULL OR ended_at >= {p}) ORDER BY launched_at",
                p,
            ),
            (since,),
        ).fetchall()
        for row in rows:
            run_id, launched_at, ended_at = row["run_id"], row["launched_at"], row["ended_at"]
            if ended_at is None and _run_ended(Path(runs_dir), run_id, None):
                ended_at = last_call(run_id) or launched_at
                if ended_at < since:
                    continue
            spans.append((run_id, launched_at, ended_at))
    except db_error_types():
        return []
    finally:
        conn.close()
    return spans


def _read_file(path: Path) -> dict[str, object] | None:
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _run_ended(runs_dir: Path, run_id: str, ended_at: Any) -> bool:
    """Edge. True when `ended_at` is set, or `<run_id>.pid` is missing, unreadable, not an int, or not a live run."""
    if ended_at is not None:
        return True
    pidfile = Path(runs_dir) / f"{run_id}.pid"
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return True
    return not epic.run_live(pid, pidfile)


def _read_calls(runs_dir: Path, run_id: str) -> list[dict]:
    opened = open_store(runs_dir)
    if opened is None:
        return []
    conn, p = opened
    try:
        rows = conn.execute(
            sql_for(
                "SELECT n.*, r.ended_at AS run_ended_at FROM node_calls n JOIN runs r ON r.run_id = n.run_id "
                "WHERE n.run_id = {p} ORDER BY n.ts, n.seq",
                p,
            ),
            (run_id,),
        ).fetchall()
    except db_error_types():
        return []
    finally:
        conn.close()
    if not rows or not _run_ended(runs_dir, run_id, rows[0]["run_ended_at"]):
        return []
    return [call_from_row(r) for r in rows]


def usage(runs_dir: Path, run_id: str) -> dict | None:
    """The usage file unchanged when it parses as an object, else the store's rows for an ended run (`ended_at` set, or no live pid), else None."""
    from_file = _read_file(Path(runs_dir) / f"{run_id}.usage.json")
    if from_file is not None:
        return from_file
    calls = _read_calls(Path(runs_dir), run_id)
    return _store_usage(run_id, calls) if calls else None


def _store_usage(run_id: str, calls: list[dict]) -> dict:
    return {"run_id": run_id, "calls": calls, "summary": summarize(calls)}


def _store_runs(runs_dir: Path, exclude: Collection[str] = (), since: str | None = None) -> dict[str, list[dict]]:
    """Every run with `node_calls` rows and a `runs` row that has ended (`ended_at` set, or no live pid), its calls ordered by ts then seq.
    Runs in `exclude` are dropped before any row becomes a call and before the pid check. With `since`, a run whose `ended_at` is set and earlier is dropped in SQL."""
    opened = open_store(runs_dir)
    if opened is None:
        return {}
    conn, p = opened
    where, params = ("WHERE r.ended_at IS NULL OR r.ended_at >= {p} ", (since,)) if since is not None else ("", ())
    try:
        rows = conn.execute(
            sql_for(
                "SELECT n.*, r.ended_at AS run_ended_at FROM node_calls n JOIN runs r ON r.run_id = n.run_id "
                + where
                + "ORDER BY n.run_id, n.ts, n.seq",
                p,
            ),
            params,
        ).fetchall()
    except db_error_types():
        return {}
    finally:
        conn.close()
    skip = frozenset(exclude)
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if row["run_id"] not in skip:
            grouped.setdefault(row["run_id"], []).append(row)
    return {
        rid: [call_from_row(r) for r in group]
        for rid, group in grouped.items()
        if _run_ended(runs_dir, rid, group[0]["run_ended_at"])
    }


def store_usages(runs_dir: Path, exclude: Collection[str] = (), since: str | None = None) -> dict[str, dict]:
    """Run id to usage for each ended store run not in `exclude`. `since` is an ISO timestamp: a run that ended before it is left out."""
    return {rid: _store_usage(rid, calls) for rid, calls in _store_runs(Path(runs_dir), exclude, since).items()}


def usages(runs_dir: Path) -> dict[str, dict]:
    """Run id to usage: every parsing usage file, then each ended store run (`ended_at` set, or no live pid) that has no file."""
    files = {
        path.name.removesuffix(".usage.json"): body
        for path in sorted(Path(runs_dir).glob("*.usage.json"))
        if (body := _read_file(path)) is not None
    }
    return {**files, **store_usages(runs_dir, exclude=files)}


def _manifest_files(runs_dir: Path) -> dict[str, list[dict[str, object]]]:
    """Run id to its parsing `<run_id>:<phase>.json` manifests, sorted by file name. The phase follows the last colon."""
    named = [
        (path.name.removesuffix(".json").rpartition(":")[0], path)
        for path in sorted(Path(runs_dir).glob("*:*.json"))
        if not path.name.endswith(".usage.json")
    ]
    by_run: dict[str, list[dict[str, object]]] = {}
    for run_id, path in named:
        if (body := _read_file(path)) is not None:
            by_run.setdefault(run_id, []).append(body)
    return by_run


def _manifest_record(record_json: Any) -> dict[str, object] | None:
    record = json_cell(record_json)
    found = record.get("manifest_record") if isinstance(record, dict) else None
    return found if isinstance(found, dict) else None


def _store_manifests(runs_dir: Path) -> dict[str, list[dict[str, object]]]:
    """Run id to the `manifest_record` of each of its `phases` rows, ended run or not, ordered by the row's ts."""
    opened = open_store(runs_dir)
    if opened is None:
        return {}
    conn, _ = opened
    try:
        rows = conn.execute("SELECT run_id, record_json FROM phases ORDER BY run_id, ts").fetchall()
    except db_error_types():
        return {}
    finally:
        conn.close()
    by_run: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        if (manifest := _manifest_record(row["record_json"])) is not None:
            by_run.setdefault(row["run_id"], []).append(manifest)
    return by_run


def phase_manifests(runs_dir: Path, run_id: str) -> list[dict[str, object]]:
    """The run's manifest files when any parse, else the `manifest_record` of its store phases, else []."""
    from_files = _manifest_files(Path(runs_dir)).get(run_id)
    return from_files or _store_manifests(Path(runs_dir)).get(run_id, [])


def all_phase_manifests(runs_dir: Path) -> dict[str, list[dict[str, object]]]:
    """Run id to phase manifests: every run with manifest files, then each store run that has none."""
    files = _manifest_files(Path(runs_dir))
    stored = {rid: ms for rid, ms in _store_manifests(Path(runs_dir)).items() if rid not in files}
    return {**files, **stored}


def _store_phase_names(runs_dir: Path, run_id: str) -> list[str]:
    opened = open_store(runs_dir)
    if opened is None:
        return []
    conn, p = opened
    try:
        rows = conn.execute(sql_for("SELECT phase_id FROM phases WHERE run_id = {p} ORDER BY ts", p), (run_id,)).fetchall()
    except db_error_types():
        return []
    finally:
        conn.close()
    return [row["phase_id"] for row in rows]


def phase_names(runs_dir: Path, run_id: str) -> list[str]:
    """Phase names of the run's manifest files, oldest mtime first; else its store phases by ts, ended run or not."""
    paths = sorted(Path(runs_dir).glob(f"{run_id}:*.json"), key=lambda p: p.stat().st_mtime)
    matches = (_PHASE_FILE.match(p.name) for p in paths)
    return [m.group(1) for m in matches if m] or _store_phase_names(Path(runs_dir), run_id)


def _json_objects(lines: Any) -> list[dict]:
    """The lines that parse as a JSON object, in order; blank, unparseable and non-object lines are dropped."""
    def parsed(line: str) -> Any:
        try:
            return json.loads(line)
        except ValueError:
            return None

    return [row for line in lines if line.strip() if isinstance(row := parsed(line), dict)]


def _loose_events(path: Path) -> list[dict]:
    try:
        return _json_objects(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return []


def _store_events(traces: Path, run_id: str, call_id: str) -> list[dict]:
    """Events of one call from `YYYY/MM/DD/<run_id>.jsonl.zst` day files, ordered by each row's `seq`."""
    try:
        import zstandard
    except ImportError as exc:
        raise TracesUnavailable("reading traces needs zstandard: install coxswain[traces]") from exc

    def rows(path: Path) -> list[dict]:
        with path.open("rb") as fh:
            reader = zstandard.ZstdDecompressor().stream_reader(fh, read_across_frames=True)
            return _json_objects(list(io.TextIOWrapper(reader, encoding="utf-8")))

    mine = [r for path in sorted(traces.glob(f"*/*/*/{run_id}.jsonl.zst")) for r in rows(path) if r.get("call_id") == call_id]
    return [r["event"] for r in sorted(mine, key=lambda r: r["seq"])]


_PARQUET_COLUMNS = ["call_id", "seq", "event"]
_NEEDS_PYARROW = "reading Parquet traces needs pyarrow: pip install 'coxswain[parquet]'"


def _parquet_call_events(rows: Sequence[Mapping[str, Any]], call_id: str) -> list[dict]:
    """Pure: the decoded `event` of each row for `call_id`, ordered by `seq`; an event that is not a JSON object is dropped."""
    mine = sorted((r for r in rows if r.get("call_id") == call_id), key=lambda r: r["seq"])
    return _json_objects(r["event"] for r in mine if isinstance(r.get("event"), str))


def task_record(runs_dir: Path, run_id: str, phase_id: str, task_id: str) -> dict[str, object] | None:
    """Edge: one task's record from the store, None when it has none or cannot say (the caller falls back to the file)."""
    found = harness_store_api().task_record(store_url_for(Path(runs_dir)), run_id, phase_id, task_id)
    return found if isinstance(found, dict) else None


def task_record_phases(runs_dir: Path, run_id: str, task: str) -> list[str]:
    """Edge: the distinct phases whose `task_records` rows name this run and task; empty with no store or on a failed read."""
    found = harness_store_api().task_record_phases(store_url_for(Path(runs_dir)), run_id, task)
    return found if isinstance(found, list) else []


def run_task_ids(runs_dir: Path, run_id: str) -> list[str]:
    """Edge: the distinct task ids whose `task_records` rows name this run; empty with no store or on a failed read."""
    found = harness_store_api().run_task_ids(store_url_for(Path(runs_dir)), run_id)
    return found if isinstance(found, list) else []


def resolve_run_id(runs_dir: Path, token: str) -> str | None:
    """Edge: the run id a short run id (`I412-7`) or an old run key names; None when it names no run or the store
    cannot say."""
    found = harness_store_api().resolve_token(store_url_for(Path(runs_dir)), token)
    return found.key if isinstance(found, harness_store_api().Resolved) and found.kind == "run" else None


def initiative_short_ids(runs_dir: Path) -> dict[str, str]:
    """Edge: slug to short id for every initiative that has one. Empty when the store cannot say."""
    found = harness_store_api().initiative_short_ids(store_url_for(Path(runs_dir)))
    return found if isinstance(found, dict) else {}


def resolve_initiative(runs_dir: Path, token: str) -> str:
    """Edge: the initiative slug for a short id; a slug, an unknown token, or any failure to read comes back unchanged."""
    by_short_id = {short_id: slug for slug, short_id in initiative_short_ids(runs_dir).items()}
    return by_short_id.get(token, token)


def task_state(rows: list[dict], initiative: str, task: str) -> str | None:
    """Pure: the `state` of the row for this initiative and task, or None when no row matches."""
    return next((row.get("state") for row in rows if row.get("initiative") == initiative and row.get("task_id") == task), None)


def rows_by_task(rows: Sequence[Mapping[str, Any]]) -> dict[tuple[Any, Any], Mapping[str, Any]]:
    """Pure: (initiative, task_id) to the first row with that key, the row `task_state` reads, so a caller asking for
    every item's row does one pass over `rows` instead of one per item."""
    index: dict[tuple[Any, Any], Mapping[str, Any]] = {}
    for row in rows:
        index.setdefault((row.get("initiative"), row.get("task_id")), row)
    return index


class _ReadFailed(Exception):
    """A full read produced no rows because the reader failed, so the cache must not store it."""


def _stamp(found: store_api.TableStamp | store_api.Failed | store_api.NotAvailable) -> Stamp | None:
    """Pure: the cache stamp of a probe, (row count, crc32 of the greatest updated_at and the row digest); None when
    the store could not say. The digest covers every column: a queue upsert keeps an older updated_at on a changed row."""
    if not isinstance(found, harness_store_api().TableStamp):
        return None
    return (found.count, zlib.crc32(json.dumps([found.newest, found.digest]).encode()))


def _rows(found: Sequence[Mapping[str, Any]] | store_api.Failed | store_api.NotAvailable) -> list[dict[str, Any]] | None:
    """Pure: the rows as plain dicts, None when the store could not say."""
    return [dict(row) for row in found] if isinstance(found, list) else None


def _versioned(cache: StampCache, key: Any, stamp: Stamp | None, read_rows: Callable[[], list[dict[str, Any]] | None]) -> list[dict[str, Any]]:
    """Edge: rows from `cache` keyed by the probe's stamp, as copies. A failed full read is [] and is never stored."""

    def full() -> list[dict[str, Any]]:
        rows = read_rows()
        if rows is None:
            raise _ReadFailed
        return rows

    try:
        return copy.deepcopy(cache.get(key, stamp, full))
    except _ReadFailed:
        return []


def work_items(runs_dir: Path, initiative: str | None = None, cache: StampCache | None = None) -> list[dict[str, Any]]:
    """Edge: `work_items` rows from the store, optionally for one initiative.

    Empty when the table or the store is missing or unreadable, so an old store reads as an empty one.
    With a cache, a probe of the row count, greatest updated_at and a digest of every row decides whether the stored rows still stand."""
    url = store_url_for(Path(runs_dir))

    def read_rows() -> list[dict[str, Any]] | None:
        return _rows(harness_store_api().work_item_rows(url, initiative))

    if cache is None:
        return read_rows() or []
    probe = _stamp(harness_store_api().work_items_stamp(url, [("initiative", initiative)]))
    return _versioned(cache, ("work_items", str(runs_dir), initiative), probe, read_rows)


def task_merge_row(runs_dir: Path, initiative: str, task: str) -> tuple[str, str | None] | None:
    """Edge: (state, merged_commit) of the task's `work_items` row; None with no store, no row or an unreadable one."""
    conn = connect_readonly(Path(runs_dir))
    if conn is None:
        return None
    sql = sql_for("SELECT state, merged_commit FROM work_items WHERE initiative = {p} AND task_id = {p}", placeholder(store_url_for(Path(runs_dir))))
    try:
        row = conn.execute(sql, (initiative, task)).fetchone()
    except db_error_types():
        return None
    finally:
        conn.close()
    return None if row is None else (row["state"], row["merged_commit"])


def task_state_of(runs_dir: Path, initiative: str, task: str) -> str | None:
    """Edge: one task's state from the store, None when the reader finds nothing, so it never raises."""
    return task_state(work_items(runs_dir, initiative), initiative, task)


@dataclass(frozen=True)
class Landed:
    rows: tuple[dict[str, str], ...]  # run_id, task_id, ts; one per (run_id, task_id)
    source: str  # "work_items" or "land_records"


def _one_land_per_key(rows: Sequence[Mapping[str, str]]) -> tuple[dict[str, str], ...]:
    """Pure: one row per (run_id, task_id), keeping the earliest ts, ordered by (ts, run_id, task_id)."""
    first = {}
    for row in sorted(rows, key=lambda r: (r["ts"], r["run_id"], r["task_id"]), reverse=True):
        first[(row["run_id"], row["task_id"])] = dict(row)
    return tuple(sorted(first.values(), key=lambda r: (r["ts"], r["run_id"], r["task_id"])))


def _done_work_item_lands(conn: Any, token: str, since: str, until: str) -> list[dict[str, str]]:
    """Edge. Done work items stamped `since <= updated_at < until`."""
    # A work item carries no run_id, so its initiative stands in as run_id; (initiative, task_id) is the table's own key.
    sql = sql_for("SELECT initiative, task_id, updated_at FROM work_items WHERE state = {p} AND updated_at >= {p} AND updated_at < {p}", token)
    rows = conn.execute(sql, ("done", since, until)).fetchall()
    return [{"run_id": r["initiative"], "task_id": r["task_id"], "ts": r["updated_at"]} for r in rows if r["updated_at"]]


def _land_record_row(row: dict, since: str, until: str) -> dict[str, str] | None:
    """Pure: a parsed `land.jsonl` row as run_id, task_id and ts when it merged (exit 0, `mark_done` reached, a task) inside the window."""
    if row.get("exit") != 0 or "mark_done" not in (row.get("steps_reached") or []):
        return None
    run, task, ts = row.get("run"), row.get("task"), row.get("ts")
    if not (task and isinstance(run, str) and isinstance(ts, str) and since <= ts < until):
        return None
    return {"run_id": run, "task_id": str(task), "ts": ts}


def _land_record_lands(runs_dir: Path, since: str, until: str) -> list[dict[str, str]]:
    """Edge. Merged lands in `runs_dir/land.jsonl` inside the window; empty when the file is absent."""
    try:
        text = (Path(runs_dir) / "land.jsonl").read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return []
    return [row for row in (_land_record_row(r, since, until) for r in parse_log(text).rows) if row is not None]


def landed_tasks(runs_dir: Path, since: str, until: str) -> Landed:
    """Edge. Tasks landed in `since <= ts < until` (ISO text): done work items, else `land.jsonl` lands; never both."""
    opened = open_store(runs_dir)
    done: list[dict[str, str]] = []
    if opened is not None:
        conn, token = opened
        try:
            done = _done_work_item_lands(conn, token, since, until)
        except db_error_types():
            done = []
        finally:
            conn.close()
    if done:
        return Landed(_one_land_per_key(done), "work_items")
    return Landed(_one_land_per_key(_land_record_lands(runs_dir, since, until)), "land_records")


_QUEUE_COLUMNS = ("kind", "initiative", "task_id", "phase", "state", "needs", "title", "surfaces", "body", "extra")
_CLAIM_KEYS = ("holder", "epoch", "expires_at")
HELD = "held"
UNAVAILABLE = "unavailable"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _queue_rows(found: Sequence[Mapping[str, Any]] | store_api.Failed | store_api.NotAvailable) -> list[dict[str, Any]] | None:
    """Pure: each row with every queue and claim key present (None when absent) and extras kept; None when the store
    could not say."""
    blank = dict.fromkeys((*_QUEUE_COLUMNS, *_CLAIM_KEYS))
    rows = _rows(found)
    return None if rows is None else [{**blank, **row} for row in rows]


def read_queue(runs_dir: Path, initiative: str | None = None, kind: str | None = None, cache: StampCache | None = None) -> list[dict[str, Any]]:
    """Edge: queue rows from the store, optionally for one initiative and kind. Empty when the table or store is unavailable.

    With a cache, a probe of the table's version decides whether the stored rows still stand."""
    url = store_url_for(Path(runs_dir))

    def read_rows() -> list[dict[str, Any]] | None:
        return _queue_rows(harness_store_api().queue_rows(url, initiative, kind))

    if cache is None:
        return read_rows() or []
    probe = _stamp(harness_store_api().work_items_stamp(url, [("initiative", initiative), ("kind", kind)]))
    return _versioned(cache, ("queue", str(runs_dir), initiative, kind), probe, read_rows)


def queue_stamp(runs_dir: Path) -> Stamp | None:
    """Edge: the version of the whole queue (row count and a digest of every row), None when the store cannot say.
    Two equal stamps mean no row changed in between."""
    return _stamp(harness_store_api().work_items_stamp(store_url_for(Path(runs_dir)), []))


def upsert_row(runs_dir: Path, row: Mapping[str, Any]) -> bool:
    """Edge: True when the store took the row, False when it could not."""
    return upsert_row_detail(runs_dir, row) == ""


def plain_row(row: Mapping[str, Any]) -> dict:
    """Pure: the row as JSON holds it. A value JSON cannot hold (a Path, a datetime) becomes its text, as it did when
    the row crossed to the store as one JSON argument."""
    return json.loads(json.dumps(dict(row), sort_keys=True, default=str))


def upsert_row_detail(runs_dir: Path, row: Mapping[str, Any]) -> str:
    """Edge: "" only when the store took the row (or already held it); otherwise a non-empty reason."""
    done = harness_store_api().upsert_queue_row(store_url_for(Path(runs_dir)), plain_row(row), _now())
    if isinstance(done, harness_store_api().Failed):
        return done.detail or f"exit {done.code}"
    if isinstance(done, harness_store_api().NotAvailable):
        return done.reason or "the store is not available"
    return ""


def release_row(runs_dir: Path, initiative: str, task_id: str, holder: str) -> bool:
    """Edge: True when the store answered; a release by a holder that did not hold the row changes nothing and is
    still True, as the harness CLI exits 0 either way. False only when the store could not say."""
    done = harness_store_api().release_queue_row(store_url_for(Path(runs_dir)), initiative, task_id, holder, _now())
    return isinstance(done, harness_store_api().Released)


def export_hashes(runs_dir: Path) -> dict[tuple[str, str], str]:
    """Edge: the sha256 of the file the export last wrote, by (initiative, task_id). A row the export never wrote is absent; empty when the store cannot say."""
    found = harness_store_api().export_hashes(store_url_for(Path(runs_dir)), _now())
    return found if isinstance(found, dict) else {}


def set_export_hash(runs_dir: Path, initiative: str, task_id: str, digest: str) -> bool:
    """Edge: True when the store recorded `digest` for this row, False when it could not."""
    done = harness_store_api().set_export_hash(store_url_for(Path(runs_dir)), initiative, task_id, digest, _now())
    return not isinstance(done, harness_store_api().Failed | harness_store_api().NotAvailable)


def claim_outcome(runs_dir: Path, initiative: str, task_id: str, holder: str, ttl_s: float, now: str) -> tuple[dict | None, str | None]:
    """Edge: `(claim, None)` when taken, else `(None, HELD)` or `(None, UNAVAILABLE)` so a caller can tell them apart."""
    done = harness_store_api().claim_queue_row(store_url_for(Path(runs_dir)), initiative, task_id, holder, int(ttl_s), now)
    if isinstance(done, harness_store_api().Claimed):
        return dict(done.claim), None
    return None, HELD if isinstance(done, harness_store_api().Held) else UNAVAILABLE


def _import_pyarrow() -> tuple[Any, Any]:
    """The only place pyarrow is imported: `(pyarrow.fs, pyarrow.parquet)`, else TracesUnavailable naming the extra."""
    try:
        import pyarrow.fs
        import pyarrow.parquet
    except ImportError as exc:
        raise TracesUnavailable(_NEEDS_PYARROW) from exc
    return pyarrow.fs, pyarrow.parquet


class ObjectStoreMisconfigured(TracesUnavailable):
    """The provider profile's object_store block cannot build a filesystem: a literal secret, or an env var that is not set."""


def s3_options(block: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    """`S3FileSystem` keyword arguments for the profile's `object_store` block, credentials read from `env`.

    `FileSystem.from_uri` knows nothing of the block, so a private endpoint is unreachable without this.
    A literal secret is refused. An endpoint with no scheme is https; path_style defaults to true."""
    literal = sorted(_LITERAL_SECRETS.intersection(block))
    if literal:
        raise ValueError(
            f"object_store holds a literal secret ({', '.join(literal)}); name an env var in access_key_env or secret_key_env"
        )
    endpoint = str(block.get("endpoint") or "")
    scheme, _, host = endpoint.partition("://") if "://" in endpoint else ("https", "", endpoint)
    optional = {
        **({"endpoint_override": host} if host else {}),
        **({"region": str(block["region"])} if block.get("region") else {}),
        **({"access_key": _from_env(block["access_key_env"], env)} if block.get("access_key_env") else {}),
        **({"secret_key": _from_env(block["secret_key_env"], env)} if block.get("secret_key_env") else {}),
    }
    return {"scheme": scheme.lower(), **optional, "force_virtual_addressing": not block.get("path_style", True)}


def _block_options(root: TracesRoot) -> dict[str, Any] | None:
    """Edge: `s3_options` for an s3:// root with an object_store block, else None. A bad block raises ObjectStoreMisconfigured."""
    if not (root.remote and root.object_store and root.url.lower().startswith("s3://")):
        return None
    try:
        return s3_options(root.object_store, os.environ)
    except ValueError as exc:
        raise ObjectStoreMisconfigured(str(exc)) from exc


def _filesystem(pafs: Any, root: TracesRoot) -> tuple[Any, str]:
    """pyarrow's own resolution for a remote URL, or an S3FileSystem from the `object_store` block for an s3:// URL that has one;
    a plain local path (possibly relative) gets the local filesystem."""
    options = _block_options(root)
    if options is not None:
        return pafs.S3FileSystem(**options), root.url[len("s3://"):].strip("/")
    if root.remote:
        return pafs.FileSystem.from_uri(root.url)
    return pafs.LocalFileSystem(), str(Path(root.url).absolute())


def _parquet_rows(root: TracesRoot, run_id: str) -> list[dict] | None:
    """Edge: the rows of the run's `YYYY/MM/DD/<run_id>.parquet` files, None when it has none.

    A local root is probed with a glob first, so a missing pyarrow only matters once a file exists. A remote root
    cannot be probed without pyarrow. Without pyarrow TracesUnavailable is raised: the harness is this same package,
    so no other interpreter could read them either."""
    name = f"{run_id}.parquet"
    if root.remote:
        pafs, pq = _import_pyarrow()
        fs, base = _filesystem(pafs, root)
        selector = pafs.FileSelector(base, recursive=True, allow_not_found=True)
        found = sorted(i.path for i in fs.get_file_info(selector) if i.type == pafs.FileType.File and i.base_name == name)
    else:
        found = sorted(str(p) for p in Path(root.url).absolute().glob(f"*/*/*/{name}"))
        if not found:
            return None
        pafs, pq = _import_pyarrow()
        fs, _ = _filesystem(pafs, root)
    if not found:
        return None
    return [row for path in found for row in pq.read_table(path, filesystem=fs, columns=_PARQUET_COLUMNS).to_pylist()]


@functools.lru_cache(maxsize=8)
def _found_parquet_rows(url: str, remote: bool, block: str, run_id: str) -> tuple[dict, ...]:
    """Raises LookupError on a miss, which lru_cache does not store: a live run's file can appear later.
    `block` is the object_store block as sorted JSON, since a mapping is not hashable; "" for none."""
    rows = _parquet_rows(TracesRoot(url, remote, json.loads(block) if block else None), run_id)
    if not rows:
        raise LookupError(run_id)
    return tuple(rows)


def _parquet_rows_once(root: TracesRoot, run_id: str) -> tuple[dict, ...] | None:
    """Edge: `_parquet_rows` read once per process for a run whose file exists; a run with none is asked again."""
    try:
        block = json.dumps(root.object_store, sort_keys=True, default=str) if root.object_store else ""
        return _found_parquet_rows(root.url, root.remote, block, run_id)
    except LookupError:
        return None


def synthetic_call_id(run_id: str, trace: object) -> str | None:
    """Pure: the id the Parquet backfill gave a call, `<run_id>-<stem of its trace path>`; None without a trace."""
    return f"{run_id}-{Path(trace).stem}" if isinstance(trace, str) and trace else None


@functools.lru_cache(maxsize=8)
def _store_ids_by_trace(runs_dir: str, run_id: str) -> dict[str, str]:
    """Edge, cached per run: the store's call id for each trace path its node_calls rows name. Calls from before call
    ids carry `legacy:<run>:<seq>` in the store, and the Parquet traces are relinked to it (graphs #445)."""
    opened = open_store(Path(runs_dir))
    if opened is None:
        return {}
    conn, p = opened
    try:
        rows = conn.execute(sql_for("SELECT call_id, detail_json FROM node_calls WHERE run_id = {p}", p), (run_id,)).fetchall()
    except db_error_types():
        return {}
    finally:
        conn.close()
    ids = {}
    for row in rows:
        detail = json_cell(row["detail_json"])
        trace = detail.get("trace") if isinstance(detail, dict) else None
        if isinstance(trace, str) and trace:
            ids[trace] = row["call_id"]
    return ids


def call_events(runs_dir: Path, run_id: str, call: Mapping[str, Any]) -> list[dict] | None:
    """A call's stream events by `call["id"]`, first source holding any wins: the run's Parquet file (for a call with
    no id, under the store's id for its trace path, then the backfill's synthetic id), then the `.jsonl.zst` day
    files, then the loose `trace` file. None when there is no trace store and no loose file."""
    call_id = str(call.get("id"))
    trace = call.get("trace")
    synthetic = synthetic_call_id(run_id, trace)
    rows, fault = _parquet_rows_or_fault(traces_root_for(Path(runs_dir)), run_id)
    if rows and (
        found := _parquet_call_events(rows, call_id)
        or (isinstance(trace, str) and trace and _parquet_call_events(rows, _store_ids_by_trace(str(runs_dir), run_id).get(trace, "")))
        or (synthetic and _parquet_call_events(rows, synthetic))
    ):
        return found
    traces = Path(runs_dir) / TRACES_DIRNAME
    stored = _store_events(traces, run_id, call_id) if traces.is_dir() else None
    if stored:
        return stored
    loose = call.get("trace")
    if isinstance(loose, str) and loose and Path(loose).is_file():
        return _loose_events(Path(loose))
    if fault is not None:
        raise fault
    return stored


def _parquet_rows_or_fault(root: TracesRoot, run_id: str) -> tuple[tuple[dict, ...] | None, ObjectStoreMisconfigured | None]:
    """Edge: `_parquet_rows_once`, with a bad object_store block returned rather than raised, so local sources are still tried."""
    try:
        return _parquet_rows_once(root, run_id), None
    except ObjectStoreMisconfigured as fault:
        return None, fault


@dataclass(frozen=True)
class ParquetCheck:
    """`readable` with the `reason`: `ok`, `pyarrow missing`, `root unreachable`, or the
    object_store error text, which names the literal secret or the unset env var."""

    readable: bool
    reason: str


def parquet_readable(traces_root: TracesRoot) -> ParquetCheck:
    """Edge: whether Parquet traces under `traces_root` can be read, for the doctor. Opens no trace file.

    A bad object_store block is reported first, by its own message, since no filesystem can be built from it."""
    try:
        _block_options(traces_root)
    except ObjectStoreMisconfigured as exc:
        return ParquetCheck(False, str(exc))
    try:
        pafs, _ = _import_pyarrow()
    except TracesUnavailable:
        return ParquetCheck(False, "pyarrow missing")
    import pyarrow

    try:
        fs, base = _filesystem(pafs, traces_root)
        reachable = fs.get_file_info(base).type == pafs.FileType.Directory
    except (OSError, ValueError, pyarrow.ArrowException):
        reachable = False
    return ParquetCheck(True, "ok") if reachable else ParquetCheck(False, "root unreachable")


def runs_started(runs_dir: Path) -> dict[str, str]:
    """Run id to the `launched_at` of every store `runs` row that has one, read in one query; {} for no store or a
    failed read. Callers that need many runs' start times use this instead of `run_started` per run, which opened one
    connection each (445 of them made `cox route context` take 24 s over the tailnet, 2026-10-06)."""
    opened = open_store(Path(runs_dir))
    if opened is None:
        return {}
    conn, p = opened
    try:
        rows = conn.execute(sql_for("SELECT run_id, launched_at FROM runs WHERE launched_at IS NOT NULL", p)).fetchall()
    except db_error_types():
        return {}
    finally:
        conn.close()
    return {row["run_id"]: row["launched_at"] for row in rows}


def run_started(runs_dir: Path, run_id: str) -> str | None:
    """The `launched_at` of the store's `runs` row, else None."""
    opened = open_store(Path(runs_dir))
    if opened is None:
        return None
    conn, p = opened
    try:
        row = conn.execute(sql_for("SELECT launched_at FROM runs WHERE run_id = {p}", p), (run_id,)).fetchone()
    except db_error_types():
        return None
    finally:
        conn.close()
    return None if row is None else row["launched_at"]
