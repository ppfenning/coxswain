"""Which task records count as completions, and when each completed. Pure: no file, clock or store is read.

Input rows are the store's `task_records` rows, the shape `dash_history` reads: `run_id`, `task_id`, `updated_at` and a
`record_json` that is JSON text or a decoded dict. What the writers leave on a record:
`cli._mark_merged` sets `merged: true` and `merged_commit` and keeps `status: approved`; a landed task carries `landed`,
`true` or mark-landed's `{at, pr}`; `status: done` is a task landed on main. A record holds no merge timestamp of its own,
so the row's `updated_at` stands for the time its `done` or merged state was written.
"""

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from itertools import groupby
from typing import Any

Row = Mapping[str, Any]
Completion = tuple[str, str, datetime]


def _record(raw: Any) -> Row:
    """A `record_json` cell as a dict; empty when it is not a JSON object."""
    if isinstance(raw, str):
        try:
            return _record(json.loads(raw))
        except ValueError:
            return {}
    return raw if isinstance(raw, Mapping) else {}


def _stamp(text: Any) -> datetime | None:
    """An ISO string as an aware datetime, a naive one read as UTC; None for anything unparseable."""
    if not isinstance(text, str):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def stamp_candidates(row: Row) -> tuple[datetime, ...]:
    """Every completion time a row shows: its `updated_at` when it is done or approved and merged, and `landed.at`."""
    record = _record(row.get("record_json"))
    status = record.get("status")
    settled = status == "done" or (status == "approved" and bool(record.get("merged")))
    landed = record.get("landed")
    found = (
        _stamp(row.get("updated_at")) if settled else None,
        _stamp(landed.get("at")) if isinstance(landed, Mapping) else None,
    )
    return tuple(stamp for stamp in found if stamp is not None)


def completions(rows: Iterable[Row]) -> list[Completion]:
    """`(run id, task id, completion time)` once per task: the earliest stamp it shows. Rows with no stamp are skipped."""
    found = sorted(
        (str(row["run_id"]), str(row["task_id"]), min(stamps))
        for row in rows
        for stamps in (stamp_candidates(row),)
        if stamps and row.get("run_id") and row.get("task_id")
    )
    return [next(group) for _, group in groupby(found, key=lambda c: c[:2])]


def day_completions(
    rows: Iterable[Row], logged: Sequence[tuple[str | None, str | None]], start: datetime, end: datetime,
) -> list[tuple[str | None, str | None]]:
    """`(run id, task id)` per task completed in `[start, end)`: each task `completions` dates there, then each `logged`
    land (the land log's `(run, task)` for that day) that no row shows a completion for at all, in log order.

    A logged task whose row completed earlier (merged yesterday, landed on main today) is not counted again, so a day
    counts what the spend windows count; the land log only stands in for a task the store holds no record of."""
    done = completions(rows)
    seen = {(run, task) for run, task, _ in done}
    dated: list[tuple[str | None, str | None]] = [(run, task) for run, task, at in done if start <= at < end]
    return [*dated, *(pair for pair in logged if pair not in seen)]
