"""Reader for the v1 run progress record: the parser is pure, `local` is the one edge that reads the file."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

VERSION = 1
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
NODE_KEYS = (
    "node", "attempt", "task", "tier", "status", "turns", "cost_usd",
    "started_at", "last_tool", "last_tool_at", "worktree", "branch",
)


@dataclass(frozen=True)
class NodeProgress:
    node: str
    attempt: int
    task: str
    tier: str
    status: str
    turns: int
    cost_usd: float | None
    started_at: str
    last_tool: str
    last_tool_at: str
    worktree: str
    branch: str | None


@dataclass(frozen=True)
class Progress:
    version: int
    run: str
    written_at: str
    nodes: tuple[NodeProgress, ...]


def _node(raw: Any) -> NodeProgress | None:
    """Pure. A node record, or None when it is not an object or lacks any key."""
    if isinstance(raw, dict) and all(key in raw for key in NODE_KEYS):
        return NodeProgress(**{key: raw[key] for key in NODE_KEYS})
    return None


def parse(text: str) -> Progress | None:
    """Pure. The progress record in `text`; None when empty, torn, not an object, or not version 1."""
    try:
        raw = json.loads(text)
    except ValueError:
        return None
    if not isinstance(raw, dict) or raw.get("version") != VERSION:
        return None
    nodes = raw.get("nodes")
    if "run" not in raw or "written_at" not in raw or not isinstance(nodes, list):
        return None
    return Progress(
        version=VERSION,
        run=raw["run"],
        written_at=raw["written_at"],
        nodes=tuple(node for node in map(_node, nodes) if node is not None),
    )


def age_s(progress: Progress, now: datetime) -> float | None:
    """Pure. Seconds from `written_at` to `now` (timezone-aware); None when `written_at` does not parse."""
    try:
        written = datetime.strptime(progress.written_at, TIME_FORMAT).replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None
    return (now - written).total_seconds()


def local(runs_dir: Path, run: str) -> Progress | None:
    """Edge. The parsed `<run>.progress.json` under `runs_dir`; None when the file is absent or unreadable."""
    try:
        text = (runs_dir / f"{run}.progress.json").read_text()
    except (OSError, UnicodeDecodeError):
        return None
    return parse(text)
