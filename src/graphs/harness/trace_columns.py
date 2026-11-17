"""Pure mapping between trace events and Parquet column rows."""

import json
from collections.abc import Mapping, Sequence
from typing import cast

COLUMNS: tuple[tuple[str, str], ...] = (
    ("run_id", "string"),
    ("call_id", "string"),
    ("seq", "int32"),
    ("day", "string"),
    ("type", "string"),
    ("subtype", "string"),
    ("tool", "string"),
    ("event", "string"),
)


def _tool(event: Mapping[str, object]) -> str | None:
    """Name of the first tool_use block of an assistant event, else None."""
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    names = (
        [
            block.get("name")
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]
        if event.get("type") == "assistant" and isinstance(content, list)
        else []
    )
    return names[0] if names else None


def to_rows(run_id: str, call_id: str, day: str, events: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """One column row per event; seq is the event's index in the list."""
    return [
        {
            "run_id": run_id,
            "call_id": call_id,
            "seq": seq,
            "day": day,
            "type": event.get("type"),
            "subtype": event.get("subtype"),
            "tool": _tool(event),
            "event": json.dumps(event, sort_keys=False, ensure_ascii=False),
        }
        for seq, event in enumerate(events)
    ]


def events_of(rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Inverse of to_rows: events in seq order."""
    return [json.loads(cast(str, row["event"])) for row in sorted(rows, key=lambda r: cast(int, r["seq"]))]
