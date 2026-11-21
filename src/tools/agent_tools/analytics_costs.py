"""Spend over time windows for the dash feed, read once from the Iceberg lake plus the store's unsynced tail.

The feed's budget block asks for the month to date and for each of the last fourteen days. Asked of the store one
window at a time, that was fifteen `SUM(cost_usd)` scans of the whole `node_calls` table on every feed build:
`node_calls.ts` has no index, so each window reads every row. This module reads the rows once and sums the windows
in memory.

Where the rows come from:
- The lake holds `node_calls` for the runs `cox lake sync` has appended. A run's calls are appended together, in one
  sync, so a run whose id the lake's `node_calls` holds is read from the lake alone.
- The store answers for every other run id in its `node_calls`: a run still going, one that ended after the last
  sync, one whose `ended_at` was stamped at or before the lake's mark after that sync ran (a backfilled `ended_at`,
  a slow clock on another machine), a call whose run has no `runs` row, and a call with no run id. The tail is
  chosen by run id, never by comparing `ended_at` with the mark, so a late-ending run is never lost. The store's run
  ids are read by a walk of `ix_node_calls_run` (one index probe per run, not a scan of the calls), and the tail's
  calls by run id through the same index.
- With no lake, a lake never synced, or one that cannot be read, one store read takes the rows from `since` on, and a
  warning says the live store answered and why. A long `cox dash --feed` warns again whenever the reason changes.

A call is in a window when its `ts`, read as an instant, is at or after `since` and before `until`. The chair's own
spend lookup, `run_store.cost_since`, compares the ISO text instead; the two agree whenever the stored `ts` and the
bounds carry the same UTC offset, which is how the harness writes them, and can differ for a call stored in another
offset within a day of a bound.

Not covered: a call written to the store for a run after that run's calls were synced. The lake holds the run, so the
late call is read from neither side.

Only the dash feed reads through here, and it imports this module when it builds the budget block. The chair's own
spend lookups (`run_store.cost_since`, from `usage_meter` and the chair loop) stay on the store and never load it."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_tools import lake_config, run_store
from agent_tools.store_url import read_provider_profile

__all__ = ["LAKE_EXTRA_MISSING", "catalog_problem", "chunks", "cost_reader", "lake_mark", "parse_rows", "sum_between", "tail_run_ids",
           "text_floor", "window_rows"]

_LOG = logging.getLogger(__name__)
# Edge state: the reason the last fallback warned about, so a changed reason warns again. Nothing pure reads it.
_last_reason: str | None = None

_LAKE_TABLE = "node_calls"
LAKE_EXTRA_MISSING = "the lake needs the optional extra `lake` (pip install 'coxswain[lake]')"
# Every distinct run id in the store's node_calls, by walking ix_node_calls_run: each step is one index probe for the
# next id, so this costs one probe per run, however many calls each holds. Both sqlite and Postgres answer MIN from
# the index. NULL run ids are not walked; `_NO_RUN` reads them.
_STORE_RUN_IDS = (
    "WITH RECURSIVE ids(run_id) AS ("
    "SELECT MIN(run_id) FROM node_calls "
    "UNION ALL SELECT (SELECT MIN(n.run_id) FROM node_calls n WHERE n.run_id > ids.run_id) FROM ids "
    "WHERE ids.run_id IS NOT NULL) "
    "SELECT run_id FROM ids WHERE run_id IS NOT NULL"
)
_NO_RUN = "SELECT ts, cost_usd FROM node_calls WHERE run_id IS NULL"
_CHUNK = 500
# No lake: every call from the floor on, in one read. `ts` is text, so the floor is a date one day early and the
# exact bound is applied in memory, as an instant.
_FROM_FLOOR = "SELECT ts, cost_usd FROM node_calls WHERE ts >= {p}"

Cost = tuple[datetime, float]
Pairs = list[tuple[object, object]]


def _instant(value: object) -> datetime | None:
    """Pure. A stored `ts` (ISO text, or a datetime from the lake) as an aware UTC instant; None when it is neither."""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def parse_rows(rows: Iterable[tuple[object, object]]) -> tuple[Cost, ...]:
    """Pure. (ts, cost_usd) pairs as (UTC instant, float). A row whose ts is not a time is dropped; a null cost is 0."""
    parsed = ((_instant(ts), float(cost) if isinstance(cost, int | float) else 0.0) for ts, cost in rows)
    return tuple((ts, cost) for ts, cost in parsed if ts is not None)


def sum_between(costs: Sequence[Cost], since: str, until: str) -> float:
    """Pure. The cost of the calls with `since <= ts < until`, the bounds ISO text read as instants."""
    low, high = _instant(since), _instant(until)
    if low is None or high is None:
        raise ValueError(f"not an ISO time: {since if low is None else until}")
    return sum(cost for ts, cost in costs if low <= ts < high)


def text_floor(since: datetime) -> str:
    """Pure. A `YYYY-MM-DD` one day before `since` in UTC: a text bound at or below `since` in any zone's ISO text."""
    return (since.astimezone(UTC) - timedelta(days=1)).date().isoformat()


def lake_mark(properties: Mapping[str, str], hwm_property: str) -> str | None:
    """Pure. The `runs.ended_at` the lake's table has synced through; None when it was never synced."""
    mark = properties.get(hwm_property)
    return mark if isinstance(mark, str) and mark else None


def catalog_problem(catalog_uri: str, catalog_file_exists: bool) -> str | None:
    """Pure. Why the lake cannot be read without creating it: a `sqlite:///` catalog whose file is absent. Reading
    must not create a catalog, so that is a reason to use the store, not to open one."""
    if catalog_uri.startswith("sqlite:///") and not catalog_file_exists:
        return f"no lake catalog at {catalog_uri.removeprefix('sqlite:///')}"
    return None


def tail_run_ids(store_ids: Iterable[str], lake_ids: Collection[str]) -> tuple[str, ...]:
    """Pure. The store's run ids the lake does not hold, sorted: the runs whose calls the store must answer for."""
    return tuple(sorted(set(store_ids) - set(lake_ids)))


def chunks(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    """Pure. `items` in consecutive slices of at most `size`, so one IN list never outgrows a backend's bind limit."""
    return (items[i:i + size] for i in range(0, len(items), size))


def _provider_profile() -> Mapping[str, Any]:
    """Edge. The provider profile the routing profile names, as `run_store` resolves the store from it; `{}` without one."""
    routing = read_provider_profile(os.environ.get("AGENT_TOOLS_PROFILE") or run_store._DEFAULT_PROFILE)
    named = routing.get("provider_profile")
    return read_provider_profile(named) if isinstance(named, str) and named else {}


def _pairs(conn: Any, token: str, template: str, params: Sequence[str]) -> Pairs:
    return [(r["ts"], r["cost_usd"]) for r in conn.execute(run_store.sql_for(template, token), tuple(params)).fetchall()]


def _store_read(runs_dir: Path, read: Callable[[Any, str], Pairs]) -> Pairs | None:
    """Edge. `read(conn, token)` on a read-only store connection; None with no store or an unreadable one."""
    opened = run_store.open_store(runs_dir)
    if opened is None:
        return None
    conn, token = opened
    try:
        return read(conn, token)
    except run_store.db_error_types():
        return None
    finally:
        conn.close()


def _tail(lake_ids: Collection[str]) -> Callable[[Any, str], Pairs]:
    """The store read for every call whose run id the lake does not hold, and every call with no run id."""

    def read(conn: Any, token: str) -> Pairs:
        store_ids = [r["run_id"] for r in conn.execute(_STORE_RUN_IDS).fetchall()]
        rows = _pairs(conn, token, _NO_RUN, ())
        for part in chunks(tail_run_ids(store_ids, lake_ids), _CHUNK):
            marks = ", ".join(["{p}"] * len(part))
            rows += _pairs(conn, token, f"SELECT ts, cost_usd FROM node_calls WHERE run_id IN ({marks})", part)
        return rows

    return read


def _lake_rows(runs_dir: Path, since: datetime) -> tuple[frozenset[str], Pairs] | str:
    """Edge. (every run id the lake's `node_calls` holds, its rows from `since` on), or why the lake could not answer."""
    config = lake_config.resolve_lake(_provider_profile(), runs_dir)
    problem = catalog_problem(config.catalog_uri, Path(config.catalog_uri.removeprefix("sqlite:///")).exists())
    if problem is not None:
        return problem
    # The lake extra is optional: without pyiceberg, or pyarrow, the lake is unavailable and the store answers. These
    # imports come first and on their own, so no later clause names a class whose import failed.
    try:
        from pyiceberg.exceptions import NoSuchTableError

        from agent_tools.lake_tables import HWM_PROPERTY, NAMESPACE
    except ImportError as exc:
        return f"{LAKE_EXTRA_MISSING} ({exc})"
    try:
        table = lake_config.load_catalog(config).load_table(f"{NAMESPACE}.{_LAKE_TABLE}")
        if lake_mark(table.properties, HWM_PROPERTY) is None:
            return f"the lake's {_LAKE_TABLE} has never been synced (cox lake sync)"
        held = table.scan(selected_fields=("run_id",)).to_arrow().column("run_id").to_pylist()
        # The row filter prunes the day partitions before `since`; the bound is our own ISO text, never input.
        window = table.scan(row_filter=f"ts >= '{since.astimezone(UTC).isoformat()}'", selected_fields=("ts", "cost_usd"))
        arrow = window.to_arrow()
    except NoSuchTableError:
        return f"the lake has no {_LAKE_TABLE} table (cox lake sync)"
    except (lake_config.LakeUnavailable, ImportError) as exc:
        return str(exc)
    except Exception as exc:  # any lake failure, a catalog, a file or a network one, falls back to the store
        return f"the lake could not be read: {type(exc).__name__}: {lake_config.redact(str(exc))}"
    rows: Pairs = list(zip(arrow.column("ts").to_pylist(), arrow.column("cost_usd").to_pylist(), strict=True))
    return frozenset(i for i in held if isinstance(i, str)), rows


def _note(reason: str | None) -> None:
    """Edge. Warn when the store answers alone for a reason not warned about last; a lake read clears the reason."""
    global _last_reason  # noqa: PLW0603
    if reason is not None and reason != _last_reason:
        _LOG.warning("analytics: %s; spend windows read the live store", reason)
    _last_reason = reason


def window_rows(runs_dir: Path, since: datetime) -> tuple[Cost, ...] | None:
    """Edge. Every call that may fall at or after `since`: the lake's, plus the store's for runs the lake does not hold;
    or the store's alone, with a warning. None when the store cannot be read: the caller then falls back as it did
    when `cost_since` gave None."""
    runs_dir = Path(runs_dir)
    found = _lake_rows(runs_dir, since)
    if isinstance(found, tuple):
        held, lake = found
        tail = _store_read(runs_dir, _tail(held))
        _note(None)
        return None if tail is None else parse_rows([*lake, *tail])
    _note(found)
    floor = text_floor(since)
    rows = _store_read(runs_dir, lambda conn, token: _pairs(conn, token, _FROM_FLOOR, (floor,)))
    return None if rows is None else parse_rows(rows)


def cost_reader(runs_dir: Path, since: datetime) -> Callable[[str, str], float | None]:
    """Edge. A `cost_between(since, until)` for `usage_window`, over one read of the calls from `since` on.

    The read happens here, once. A window that starts before `since` is refused: it would silently undercount."""
    costs = window_rows(runs_dir, since)

    def cost_between(low: str, high: str) -> float | None:
        if costs is None:
            return None
        start = _instant(low)
        if start is None or start < since.astimezone(UTC):
            raise ValueError(f"window {low} starts before the rows read from {since.isoformat()}")
        return sum_between(costs, low, high)

    return cost_between
