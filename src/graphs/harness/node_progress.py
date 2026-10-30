"""Per-node progress record, built from claude stream events.

The Tracker is pure: it never reads the time, the caller passes a clock. The
only I/O is `write_atomic`. The record shape is v1, pinned by
src/tools/tests/fixtures/node_progress_v1.json.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The clock returns a datetime; naive values are taken as UTC.
Clock = Callable[[], datetime]


def _iso(moment: datetime) -> str:
    aware = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Tracker:
    """Nodes seen so far, keyed by (node, attempt). One lock guards every method."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._nodes: dict[tuple[str, int], dict[str, Any]] = {}

    def start(
        self,
        node: str,
        attempt: int,
        task: str,
        tier: str,
        worktree: str,
        branch: str | None,
    ) -> None:
        with self._lock:
            self._nodes[(node, attempt)] = {
                "node": node,
                "attempt": attempt,
                "task": task,
                "tier": tier,
                "status": "running",
                "turns": 0,
                "cost_usd": None,
                "started_at": _iso(self._clock()),
                "last_tool": None,
                "last_tool_at": None,
                "worktree": worktree,
                "branch": branch,
            }

    def observe(self, node: str, attempt: int, event: Mapping[str, Any]) -> None:
        with self._lock:
            record = self._nodes.get((node, attempt))
            if record is None:
                return
            kind = event.get("type")
            if kind == "assistant":
                record["turns"] += 1
                message = event.get("message") or {}
                for item in message.get("content") or []:
                    if isinstance(item, Mapping) and item.get("type") == "tool_use":
                        record["last_tool"] = item.get("name")
                        record["last_tool_at"] = _iso(self._clock())
            elif kind == "result":
                record["status"] = "failed" if event.get("is_error") else "done"
                if event.get("total_cost_usd") is not None:
                    record["cost_usd"] = event["total_cost_usd"]

    def fail(self, node: str, attempt: int) -> None:
        with self._lock:
            record = self._nodes.get((node, attempt))
            if record is not None:
                record["status"] = "failed"

    def snapshot(self, run: str) -> dict[str, Any]:
        with self._lock:
            return {
                "version": 1,
                "run": run,
                "written_at": _iso(self._clock()),
                "nodes": [dict(record) for record in self._nodes.values()],
            }


def write_atomic(path: Path, record: Mapping[str, Any]) -> None:
    """Write the record beside `path`, then replace it, so no reader sees a torn file.

    The only I/O in this module.
    """
    path = Path(path)
    text = json.dumps(record, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
