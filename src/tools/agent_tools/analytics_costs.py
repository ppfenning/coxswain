"""Spend over time windows for the dash feed, read once from the Iceberg lake plus the store's unsynced tail.

The feed's budget block asks for the month to date and for each of the last fourteen days. Asked of the store one
window at a time, that was fifteen `SUM(cost_usd)` scans of the whole `node_calls` table on every feed build:
`node_calls.ts` has no index, so each window reads every row. This module reads the rows once and sums the windows
in memory.

Where the rows come from:
- The lake holds `node_calls` for every run that had ended when `cox lake sync` last ran; its mark (`HWM_PROPERTY` on
  the lake's `node_calls` table) is the `runs.ended_at` it has synced through (`lake_sync`).
- The store answers only for the runs the lake does not hold: not ended, or ended after the mark. That read goes
  through `runs`, the small table, and `ix_node_calls_run`, so it reads those runs' calls and nothing else.
- With no lake, no mark, or a lake that cannot be read, one store read takes the rows from `since` on, and a warning
  says the live store answered and why (once per process).

A call is in a window when its `ts`, read as an instant, is at or after `since` and before `until`. Calls whose run
has no `runs` row are in neither half: `lake_sync` joins on `runs`, and the tail read does too.

Only the dash feed reads through here. The chair's own spend lookups (`run_store.cost_since`, from `usage_meter` and
the chair loop) stay on the store and never touch the lake."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_tools import lake_config, run_store
from agent_tools.store_url import read_provider_profile

__all__ = ["Rows", "catalog_problem", "cost_reader", "lake_mark", "parse_rows", "sum_between", "text_floor", "window_rows"]

_LOG = logging.getLogger(__name__)
# Edge state: the once-per-process flag for the fallback warning. Nothing in the pure core reads it.
_warned = False

_LAKE_TABLE = "node_calls"
# The tail: calls of the runs the lake does not hold. `runs` is read whole (it is small); its calls come by run_id.
_TAIL = (
    "SELECT n.ts AS ts, n.cost_usd AS cost_usd FROM node_calls n JOIN runs r ON r.run_id = n.run_id "
    "WHERE r.ended_at IS NULL OR r.ended_at > {p}"
)
# No lake: every call from the floor on, in one read. `ts` is text, so the floor is a date one day early and the
# exact bound is applied in memory, as an instant.
_FROM_FLOOR = "SELECT ts, cost_usd FROM node_calls WHERE ts >= {p}"

Cost = tuple[datetime, float]


@dataclass(frozen=True)
class Rows:
    """The (ts, cost_usd) of every call a window may need, and where they came from: `lake` or `store`.

    `note` says why the store answered alone; None when the lake did its part."""

    costs: tuple[Cost, ...]
    source: str
    note: str | None = None


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


def _provider_profile() -> Mapping[str, Any]:
    """Edge. The provider profile the routing profile names, as `run_store` resolves the store from it; `{}` without one."""
    routing = read_provider_profile(os.environ.get("AGENT_TOOLS_PROFILE") or run_store._DEFAULT_PROFILE)
    named = routing.get("provider_profile")
    return read_provider_profile(named) if isinstance(named, str) and named else {}


def _store_rows(runs_dir: Path, template: str, param: str) -> list[tuple[object, object]] | None:
    """Edge. (ts, cost_usd) of `template` on the store; None with no store or an unreadable one."""
    opened = run_store.open_store(runs_dir)
    if opened is None:
        return None
    conn, token = opened
    try:
        return [(r["ts"], r["cost_usd"]) for r in conn.execute(run_store.sql_for(template, token), (param,)).fetchall()]
    except run_store.db_error_types():
        return None
    finally:
        conn.close()


def _lake_rows(runs_dir: Path, since: datetime) -> tuple[str, list[tuple[object, object]]] | str:
    """Edge. (mark, rows) from the lake's `node_calls` from `since` on, or the reason the lake could not answer."""
    config = lake_config.resolve_lake(_provider_profile(), runs_dir)
    problem = catalog_problem(config.catalog_uri, Path(config.catalog_uri.removeprefix("sqlite:///")).exists())
    if problem is not None:
        return problem
    try:
        from pyiceberg.exceptions import NoSuchTableError

        from agent_tools.lake_tables import HWM_PROPERTY, NAMESPACE

        table = lake_config.load_catalog(config).load_table(f"{NAMESPACE}.{_LAKE_TABLE}")
        mark = lake_mark(table.properties, HWM_PROPERTY)
        if mark is None:
            return f"the lake's {_LAKE_TABLE} has never been synced (cox lake sync)"
        # The row filter prunes the day partitions before `since`; the bound is our own ISO text, never input.
        scan = table.scan(row_filter=f"ts >= '{since.astimezone(UTC).isoformat()}'", selected_fields=("ts", "cost_usd"))
        arrow = scan.to_arrow()
    except NoSuchTableError:
        return f"the lake has no {_LAKE_TABLE} table (cox lake sync)"
    except (lake_config.LakeUnavailable, ImportError) as exc:
        return str(exc)
    except Exception as exc:  # any lake failure, a catalog, a file or a network one, falls back to the store
        return f"the lake could not be read: {type(exc).__name__}: {lake_config.redact(str(exc))}"
    return mark, list(zip(arrow.column("ts").to_pylist(), arrow.column("cost_usd").to_pylist(), strict=True))


def _warn_once(message: str) -> None:
    """Edge. Log `message` the first time in this process and never again."""
    global _warned  # noqa: PLW0603
    if not _warned:
        _warned = True
        _LOG.warning("analytics: %s; spend windows read the live store", message)


def window_rows(runs_dir: Path, since: datetime) -> Rows | None:
    """Edge. Every call from `since` on, from the lake plus the store's tail; or from the store alone, with the reason.

    None when the store cannot be read: the caller then falls back as it did when `cost_since` gave None."""
    runs_dir = Path(runs_dir)
    found = _lake_rows(runs_dir, since)
    if isinstance(found, tuple):
        mark, lake = found
        tail = _store_rows(runs_dir, _TAIL, mark)
        return None if tail is None else Rows(parse_rows([*lake, *tail]), "lake")
    _warn_once(found)
    rows = _store_rows(runs_dir, _FROM_FLOOR, text_floor(since))
    return None if rows is None else Rows(parse_rows(rows), "store", found)


def cost_reader(runs_dir: Path, since: datetime) -> Callable[[str, str], float | None]:
    """Edge. A `cost_between(since, until)` for `usage_window`, over one read of the calls from `since` on.

    The read happens here, once. A window that starts before `since` is refused: it would silently undercount."""
    rows = window_rows(runs_dir, since)

    def cost_between(low: str, high: str) -> float | None:
        if rows is None:
            return None
        start = _instant(low)
        if start is None or start < since.astimezone(UTC):
            raise ValueError(f"window {low} starts before the rows read from {since.isoformat()}")
        return sum_between(rows.costs, low, high)

    return cost_between
