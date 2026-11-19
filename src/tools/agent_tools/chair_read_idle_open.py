"""The open idle-stall item: `open_signature` and `open_diagnosis` of `IdleStallInputs`.

A `deferred_land_deadlock` needs_chair row replaces the idle_stall line for the same stall, so it counts as the open item too.
An `idle_ready` row does the same, or the next tick would read no open signature and send again.

An item is open exactly as the dashboard's open needs-chair count decides it: an unacknowledged courier entry with a
`coxswain://needs_chair/<id>` ref (`dash_feed.open_needs_chair_ids`). The `<id>` of an idle-stall item is the
signature the planner recorded, so a `chair_actions` row is open when its signature is among those ids.
unknown: nothing writes that courier entry yet, so until a writer sends one with the signature as id, no stall reads open.
A row's `signature` and `reason` sit beside `kind` and `cause`, or inside `action_json` as the store keeps them.
"""

import json
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from agent_tools import courier
from agent_tools.chair_action_rows import row_view
from agent_tools.chair_idle_ready import IDLE_READY_CAUSE
from agent_tools.chair_read_stale import read_chair_actions
from agent_tools.chair_types import DEADLOCK_CAUSE
from agent_tools.dash_chair_today import row_ts
from agent_tools.dash_feed import courier_blob, iso_ts_row, open_needs_chair_ids
from core.jsontypes import is_dict

CAUSE = "idle_stall"
_OLDEST = datetime.min.replace(tzinfo=UTC)


def _fields(row: Mapping[str, object]) -> Mapping[str, object]:
    """The row with its `action_json` fields laid over it; a Postgres dict or a SQLite JSON string, else nothing."""
    raw = row.get("action_json")
    parsed: object
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        parsed = None
    return {**row, **parsed} if is_dict(parsed) else row


def _flat(row: Mapping[str, object]) -> Mapping[str, object]:
    """`_fields` of the row with an ISO `ts`; worked out once per kept row (`chair_action_rows`)."""
    return _fields(iso_ts_row(dict(row)))


def open_idle_stall(
    rows: Sequence[Mapping[str, object]], open_items: Collection[str]
) -> tuple[str | None, str | None]:
    """(signature, reason) of the newest open `idle_stall`, `deferred_land_deadlock` or `idle_ready` needs_chair row, else (None, None); a resolved one is not open."""
    flat = [row_view(r, _flat) for r in rows]
    mine = [
        r for r in flat
        if r.get("kind") == "needs_chair" and r.get("cause") in (CAUSE, DEADLOCK_CAUSE, IDLE_READY_CAUSE) and r.get("signature") in open_items
    ]
    newest = max(enumerate(mine), key=lambda p: (row_ts(p[1]) or _OLDEST, p[0]), default=None)
    return (str(newest[1]["signature"]), str(newest[1].get("reason") or "")) if newest is not None else (None, None)


def read_idle_open(ws: Path, actions: Sequence[Mapping[str, object]] | None = None) -> tuple[str | None, str | None]:
    """Edge. Never raises: no store, no table, an unreadable courier file or a database error gives (None, None).

    `actions` is the tick's shared `FactsDeps.actions()` rows. `needs_chair` is a kept kind, so the window never drops an open row."""
    try:
        # None is the caller that has not been wired to the shared rows yet; it still reads its own.
        rows = actions if actions is not None else read_chair_actions(ws / "runs")
        return open_idle_stall(rows, open_needs_chair_ids(courier.inbox(courier_blob(ws))))
    except Exception:  # a tick must survive any unreadable source
        return (None, None)
