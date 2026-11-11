"""Pure detector for intakes whose newest decompose launch hit a missing idea file."""

import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

# `aligned` is imported from the pure backoff module, not chair_facts, so this module stays free of the facts edge.
from agent_tools.chair_land_backoff import aligned

# Wrong belief: a missing idea file creates a run, so the empty-decompose streak counts it. It creates none.
MISSING_IDEA_BACKOFF_S = 3600
MISSING_IDEA_MARKER = "no idea file at"

Row = Mapping[str, Any]


def _doc(row: Row) -> dict[str, Any] | None:
    """The row with its `action_json` merged over it, like chair_facts._action_doc; None when it cannot be read."""
    try:
        raw = row.get("action_json")
        doc = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        return {**row, **(doc if isinstance(doc, dict) else {})}
    except (AttributeError, TypeError, ValueError):
        return None


def _launch(row: Row, now: datetime) -> tuple[datetime, dict[str, Any], list[str]] | None:
    """(stamp, doc, intake paths) for a readable launch_decompose row with a parseable `ts`; else None."""
    doc = _doc(row)
    if doc is None or doc.get("kind") != "launch_decompose":
        return None
    paths = doc.get("intake_ids")
    if not isinstance(paths, (list, tuple)) or not all(isinstance(p, str) for p in paths):
        return None
    try:
        return aligned(str(doc.get("ts")), now), doc, list(paths)
    except ValueError:
        return None


def missing_idea_paths(action_rows: Iterable[Row], now: datetime) -> frozenset[str]:
    """Intake paths whose newest launch_decompose failed with 'no idea file at' less than MISSING_IDEA_BACKOFF_S ago.

    A later launch_decompose for the path with any other outcome removes it at once; a malformed row is ignored.
    """
    launches = sorted(
        (found for found in (_launch(row, now) for row in action_rows) if found is not None),
        key=lambda found: found[0],
    )
    newest = {path: (at, doc) for at, doc, paths in launches for path in paths}
    window = timedelta(seconds=MISSING_IDEA_BACKOFF_S)
    return frozenset(
        path
        for path, (at, doc) in newest.items()
        if doc.get("status") == "failed"
        and MISSING_IDEA_MARKER in str(doc.get("reason") or "")
        and timedelta(0) <= now - at < window
    )
