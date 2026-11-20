"""Lands waiting to be judged, kept as `chair_actions` rows: a `revert_watch` per land, a `revert_watch_resolved` per verdict.

The recorder appends, so the store holds one row per `record_watch` call. `watches` is the one-per-commit view; every
reader goes through it rather than repeating the dedupe.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime

from agent_tools.chair_action_rows import row_view
from agent_tools.chair_types import LandWatch
from core.jsontypes import is_dict

Row = Mapping[str, object]
Write = Callable[[Mapping[str, object]], None]  # the chair's action recorder, `chair_read_record.recorder(...)`
Read = Callable[[], Sequence[Row]]  # the `chair_actions` rows, as `chair_facts.FactsDeps.actions` takes them

WATCH_KIND = "revert_watch"
RESOLVED_KIND = "revert_watch_resolved"
OUTCOMES = ("reverted", "held")
# The store's record-action refuses a line without ts, epoch, kind and status; the recorder adds ts and epoch.
STATUS = "recorded"
_NEVER = datetime.min.replace(tzinfo=UTC)


def _columns_over_doc(row: Row) -> Row:
    raw = row.get("action_json")
    doc = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    return {**(doc if is_dict(doc) else {}), **{k: v for k, v in row.items() if k != "action_json"}}


def _doc(row: Row) -> Row:
    """The `action_json` keys beneath the row's own columns, so the store's `ts` column wins over the line's; worked out
    once per kept row (`chair_action_rows`)."""
    return row_view(row, _columns_over_doc)


def watch_rows(rows: Sequence[Row]) -> list[Row]:
    """Pure. The watch and verdict rows among `rows`, in their order: every reader here gives the same answer from
    them as from all the rows, and a tick's window holds some 180,000 rows to their thousand."""
    return [row for row in rows if _doc(row).get("kind") in (WATCH_KIND, RESOLVED_KIND)]


def _docs(read: Read, kind: str) -> list[Row]:
    return [d for d in map(_doc, read()) if d.get("kind") == kind]


def _str(doc: Row, key: str) -> str:
    """The str a watch row carries under `key`; KeyError when absent, as indexing raised."""
    value = doc[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} of a revert watch row is not a string: {value!r}")
    return value


def _parsed(raw: object) -> datetime:
    if isinstance(raw, datetime):
        return raw
    if raw:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return _NEVER


def _when(doc: Row) -> datetime:
    """A Postgres datetime or an ISO string with `Z`, as aware UTC; a row with no ts sorts first."""
    parsed = _parsed(doc.get("ts"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _watch(doc: Row) -> LandWatch:
    pr = doc["pr"]
    if not isinstance(pr, int | str):
        raise TypeError(f"pr of a revert watch row is neither a number nor text: {pr!r}")
    return LandWatch(
        initiative=_str(doc, "initiative"), phase=_str(doc, "phase"), repo=_str(doc, "repo"), pr=pr, commit=_str(doc, "commit")
    )


def record_watch(write: Write, watch: LandWatch) -> None:
    write(
        {
            "kind": WATCH_KIND,
            "status": STATUS,
            "initiative": watch["initiative"],
            "phase": watch["phase"],
            "repo": watch["repo"],
            "pr": watch["pr"],
            "commit": watch["commit"],
        }
    )


def resolve_watch(write: Write, commit: str, outcome: str) -> None:
    """Outcome is `reverted` or `held`; the recorder's `ts` is the resolution time `outcomes` orders by."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, not {outcome!r}")
    write({"kind": RESOLVED_KIND, "status": STATUS, "commit": commit, "outcome": outcome})


def watches(read: Read) -> dict[str, LandWatch]:
    """Commit to its watch, one per commit however many times it was recorded, in first-recorded order."""
    return {_str(d, "commit"): _watch(d) for d in _docs(read, WATCH_KIND)}


def pending_watches(read: Read) -> list[LandWatch]:
    resolved = {_str(d, "commit") for d in _docs(read, RESOLVED_KIND)}
    return [w for c, w in watches(read).items() if c not in resolved]


def outcomes(read: Read) -> dict[str, list[str]]:
    """Oldest resolution first; a commit counts once, at its earliest verdict; equal times order by commit."""
    watched = watches(read)
    by_time = sorted(_docs(read, RESOLVED_KIND), key=lambda d: (_when(d), _str(d, "commit")))
    earliest = {_str(d, "commit"): d for d in reversed(by_time) if _str(d, "commit") in watched}
    verdicts = sorted(earliest.values(), key=lambda d: (_when(d), _str(d, "commit")))
    initiatives = dict.fromkeys(watched[_str(d, "commit")]["initiative"] for d in verdicts)
    return {i: [_str(d, "outcome") for d in verdicts if watched[_str(d, "commit")]["initiative"] == i] for i in initiatives}
