"""The v1 node progress record: claude stream events in, one JSON-able dict out.

Time is an argument: the tracker is handed a clock that returns an ISO-8601 string and never reads one.
`write_atomic` is the only function here that touches the disk.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["Tracker", "write_atomic"]

_KEY = tuple[str, int]


def _tool_names(event: dict[str, Any]) -> list[str]:
    content = (event.get("message") or {}).get("content")
    items = content if isinstance(content, list) else []
    return [i.get("name") or "" for i in items if isinstance(i, dict) and i.get("type") == "tool_use"]


class Tracker:
    """The nodes seen so far, in the order they were started, keyed by (node, attempt).

    One lock guards every method, so the runner's reader thread may call `observe` while the
    heartbeat thread calls `snapshot`. Records are replaced, never edited, so a snapshot shares nothing.
    """

    def __init__(self, clock: Callable[[], str]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._nodes: dict[_KEY, dict[str, Any]] = {}

    def start(self, node: str, attempt: int, task: str, tier: str, worktree: str, branch: str | None) -> None:
        record = {
            "node": node,
            "attempt": attempt,
            "task": task,
            "tier": tier,
            "status": "running",
            "turns": 0,
            "cost_usd": None,
            "started_at": self._clock(),
            "last_tool": None,
            "last_tool_at": None,
            "worktree": worktree,
            "branch": branch,
        }
        with self._lock:
            self._nodes[(node, attempt)] = record

    def observe(self, node: str, attempt: int, event: dict[str, Any]) -> None:
        """Fold one stream event into a running node. Anything else is ignored."""
        kind = event.get("type")
        with self._lock:
            current = self._nodes.get((node, attempt))
            if current is None or current["status"] != "running":
                return
            if kind == "assistant":
                tools = _tool_names(event)
                change = {"turns": current["turns"] + 1}
                if tools:
                    change = {**change, "last_tool": tools[-1], "last_tool_at": self._clock()}
            elif kind == "result":
                failed = bool(event.get("is_error")) or str(event.get("subtype", "")).startswith("error")
                change = {"status": "failed" if failed else "done", "cost_usd": event.get("total_cost_usd")}
            else:
                return
            self._nodes[(node, attempt)] = {**current, **change}

    def fail(self, node: str, attempt: int) -> None:
        """The call raised or timed out."""
        with self._lock:
            current = self._nodes.get((node, attempt))
            if current is not None:
                self._nodes[(node, attempt)] = {**current, "status": "failed"}

    def snapshot(self, run: str) -> dict[str, Any]:
        with self._lock:
            nodes = [dict(n) for n in self._nodes.values()]
            written_at = self._clock()
        return {"version": 1, "run": run, "written_at": written_at, "nodes": nodes}


def write_atomic(path: str | Path, record: dict[str, Any]) -> None:
    """Write beside the target, then replace it, so a reader sees the old file or the new one and never half."""
    target = Path(path)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
