"""Per-turn usage records from a run's lane traces, for the dash feed's live turns list.

Wrong belief to avoid: "lane trace events carry a timestamp". A loose `<run>-trace/<role>-<n>.jsonl` file holds
stream-json events with no time on them (see harness/store_backfill_traces.py), so on such a trace this reader
returns no turns. A time is never taken from the file's mtime: it would stamp every turn of a node with one
instant. The lane stays empty until a real per-turn time source exists, which is a planning decision.

Wrong belief to avoid: "one event is one turn". The CLI writes one event per content block and each repeats the
message's usage, so a turn is one message id. Of the repeats the one with the most output tokens is kept,
because the first block can carry a partial output count.

A cost of 0.0 means the model had no price or the turn was free; the turn record has no field to tell them apart.
The caller stamps `task` and `node`: a trace directory is per run, so nothing here proves a file belongs to a task.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_tools.dash_transcript_cursor import CursorState, KeyedTurn, advance
from agent_tools.records import load_trace

__all__ = ["advance_lane_cursor", "read_lane_turns", "turns_from_events"]

_PER_MILLION = 1_000_000
_TRACE_STEM = re.compile(r"^(?P<role>.+)-\d+$")  # a role may hold hyphens: review-adversary-1

# trace usage field -> (record field, price key in the catalog)
_TOKEN_FIELDS = (
    ("input_tokens", "input_tokens", "input"),
    ("output_tokens", "output_tokens", "output"),
    ("cache_read_input_tokens", "cache_read_tokens", "cache_read"),
    ("cache_creation_input_tokens", "cache_write_tokens", "cache_write"),
)


def _iso_utc(raw: Any) -> str | None:
    """`raw` as YYYY-MM-DDTHH:MM:SSZ; a time with no zone reads as UTC; None when it is not a time."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return (
        moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if moment.tzinfo
        else moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _message(event: Mapping[str, Any]) -> Mapping[str, Any]:
    message = event.get("message")
    return message if isinstance(message, Mapping) else {}


def _usage(event: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The `message.usage` block when it carries at least one integer token count."""
    usage = _message(event).get("usage")
    if not isinstance(usage, Mapping):
        return None
    return usage if any(isinstance(usage.get(field), int) for field, _, _ in _TOKEN_FIELDS) else None


def _message_key(event: Mapping[str, Any]) -> str | None:
    """The message id, else the requestId; None when the event names neither and so counts alone."""
    for candidate in (_message(event).get("id"), event.get("requestId")):
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _cost_usd(event: Mapping[str, Any], usage: Mapping[str, Any], price: Mapping[str, float]) -> float:
    """The event's own cost when it has one, else tokens times `price`; an empty price costs 0.0."""
    own = event.get("cost_usd")
    if isinstance(own, int | float) and not isinstance(own, bool):
        return float(own)
    return sum(int(usage.get(field) or 0) * price.get(key, 0.0) for field, _, key in _TOKEN_FIELDS) / _PER_MILLION


def turns_from_events(
    events: Sequence[Mapping[str, Any]],
    host: str,
    run: str,
    initiative: str,
    task: str,
    node: str,
    prices: Mapping[str, Mapping[str, float]],
    *,
    agent_id: str = "",
) -> list[dict[str, Any]]:
    """Pure: one record per message with its own `timestamp` and a usage block; any other event is skipped.

    `prices` is $ per million tokens by model."""
    rows = [(event, _iso_utc(event.get("timestamp")), _usage(event)) for event in events]
    kept = [(event, ts, usage) for event, ts, usage in rows if ts is not None and usage is not None]
    keys = [_message_key(event) for event, _, _ in kept]
    output = [int(usage.get("output_tokens") or 0) for _, _, usage in kept]
    # most output wins per message id; on a tie the earlier event
    best_at = {keys[i]: i for i in sorted(range(len(kept)), key=lambda i: (output[i], -i)) if keys[i] is not None}

    def turn(event: Mapping[str, Any], ts: str, usage: Mapping[str, Any]) -> dict[str, Any]:
        model = str(_message(event).get("model") or event.get("model") or "")
        return {
            "ts": ts,
            "host": host,
            "source": "lane",
            "kind": node,
            "agent_id": agent_id,
            "model": model,
            "run": run,
            "initiative": initiative,
            "task": task,
            **{record: int(usage.get(field) or 0) for field, record, _ in _TOKEN_FIELDS},
            "cost_usd": _cost_usd(event, usage, prices.get(model) or {}),
        }

    return [turn(*row) for i, row in enumerate(kept) if keys[i] is None or best_at[keys[i]] == i]


def _file_turns(path: Path, prices: Mapping[str, Mapping[str, float]], **stamp: str) -> list[dict[str, Any]]:
    """Edge: one trace file's turns; an unreadable file yields none."""
    try:
        events = load_trace(path)
    except OSError, UnicodeDecodeError:
        return []
    return turns_from_events(events, prices=prices, agent_id=path.stem, **stamp)


def _trace_paths(runs_dir: Path | str, run: str, node: str) -> list[Path]:
    """Edge: the run's `<node>-<n>.jsonl` trace files; other roles' files are another node's turns."""
    trace_dir = Path(runs_dir) / f"{run}-trace"
    try:
        return sorted(p for p in trace_dir.glob("*.jsonl") if (m := _TRACE_STEM.match(p.stem)) and m["role"] == node)
    except OSError:
        return []


def _read_from(path: Path, offset: int) -> bytes:
    """Edge: the bytes of `path` from `offset` to its end; an unreadable file yields none."""
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            return handle.read()
    except OSError:
        return b""


def _loads(line: str) -> dict[str, Any] | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def _parse_lines(path: Path, lines: list[str]) -> list[KeyedTurn]:
    """Pure: the events `turns_from_events` can use, each keyed by its line text.

    The cursor holds raw events, not built turns, so the most-output repeat of a message is still chosen at read time.
    Byte-identical lines share a key and collapse to one, which only matters for an event naming no message id."""
    events = ((line, _loads(line)) for line in lines)
    return [
        (line, event)
        for line, event in events
        if event is not None and _iso_utc(event.get("timestamp")) is not None and _usage(event) is not None
    ]


def advance_lane_cursor(runs_dir: Path | str, run: str, node: str, cursor: CursorState | None = None) -> CursorState:
    """Edge: the cursor after the node's traces as they are now; an unchanged file reads nothing, a grown one its new bytes.

    Call this for the new cursor, then pass that to `read_lane_turns`."""
    stats: dict[Path, tuple[int, int]] = {}
    for path in _trace_paths(runs_dir, run, node):
        try:
            seen = path.stat()
        except OSError:
            continue
        stats[path] = (seen.st_size, seen.st_mtime_ns)
    return advance(cursor or CursorState(), stats, _read_from, _parse_lines)


def read_lane_turns(
    runs_dir: Path | str,
    run: str,
    host: str,
    initiative: str,
    task: str,
    node: str,
    prices: Mapping[str, Mapping[str, float]] | None = None,
    *,
    cursor: CursorState | None = None,
) -> list[dict[str, Any]]:
    """Edge: turns from the run's `<node>-<n>.jsonl` trace files; other roles' files are another node's turns.

    With a `cursor` only bytes the cursor has not seen are read. The updated cursor comes from `advance_lane_cursor`."""
    stamp = {"host": host, "run": run, "initiative": initiative, "task": task, "node": node}
    if cursor is not None:
        return [
            turn
            for path, seen in advance_lane_cursor(runs_dir, run, node, cursor).paths
            for turn in turns_from_events(
                [event for _, event in seen.turns], prices=prices or {}, agent_id=path.stem, **stamp
            )
        ]
    return [turn for path in _trace_paths(runs_dir, run, node) for turn in _file_turns(path, prices or {}, **stamp)]
