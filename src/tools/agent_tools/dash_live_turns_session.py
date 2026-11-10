"""Turn records from Claude session and subagent transcripts: a pure parser and a thin directory reader."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from agent_tools.chair_activity_read import _read_lines
from agent_tools.dash_transcript_cursor import CursorState, advance
from agent_tools.stats_chair import _usage_key, chair_cost

_USAGE_TO_TURN = (
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("cache_read_input_tokens", "cache_read_tokens"),
    ("cache_creation_input_tokens", "cache_write_tokens"),
)


def _keyed_turn(
    line: str, labels: Mapping[str, str], prices: Mapping[str, Mapping[str, float]]
) -> tuple[str, dict] | None:
    """(`chair_cost` dedupe key, turn record) for an assistant line with timestamp, usage and model; None for any other line or non-int counts."""
    try:
        entry = json.loads(line)
    except TypeError, ValueError:
        return None
    message = entry.get("message") if isinstance(entry, dict) and entry.get("type") == "assistant" else None
    usage = message.get("usage") if isinstance(message, dict) else None
    counts_ok = isinstance(usage, dict) and all(type(usage.get(f) or 0) is int for f, _ in _USAGE_TO_TURN)
    ts = entry.get("timestamp") if counts_ok else None
    model = message.get("model") if counts_ok else None
    if not (isinstance(ts, str) and ts and isinstance(model, str) and model):
        return None
    return _usage_key(entry, message, line), {
        "ts": ts,
        **labels,
        "model": model,
        "run": "",
        "initiative": "",
        "task": "",
        **{turn_key: usage.get(field) or 0 for field, turn_key in _USAGE_TO_TURN},
        "cost_usd": float(chair_cost([line], prices)),
    }


def keyed_turns_from_lines(
    lines: Iterable[str],
    host: str,
    source: str,
    kind: str,
    agent_id: str,
    prices: Mapping[str, Mapping[str, float]],
) -> list[tuple[str, dict]]:
    """(`chair_cost` key, turn) per assistant message, the first line per key; `prices` is $ per million tokens by model."""
    labels = {"host": host, "source": source, "kind": kind, "agent_id": agent_id}
    keyed = [t for t in (_keyed_turn(line, labels, prices) for line in lines) if t is not None]
    first = dict(reversed([(key, i) for i, (key, _) in enumerate(keyed)]))
    return [item for i, item in enumerate(keyed) if first[item[0]] == i]


def turns_from_lines(
    lines: Iterable[str],
    host: str,
    source: str,
    kind: str,
    agent_id: str,
    prices: Mapping[str, Mapping[str, float]],
) -> list[dict]:
    """One record per assistant message, the first line per `chair_cost` key; `prices` is $ per million tokens by model."""
    return [turn for _, turn in keyed_turns_from_lines(lines, host, source, kind, agent_id, prices)]


def _identity(path: Path, chair_session: str) -> tuple[str, str, str]:
    """(source, kind, agent_id) for a transcript path: `subagents/agent-<id>.jsonl` or a main session file."""
    if path.parent.name == "subagents":
        return "subagent", "subagent", path.stem.removeprefix("agent-")
    return "session", "chair" if path.stem == chair_session else "operator", ""


def _transcripts(root: Path) -> list[Path]:
    """Main and subagent transcripts; session ids hold no dot, so `<id>.bak.jsonl` neighbours are not sessions."""
    mains = (p for p in root.glob("*/*.jsonl") if "." not in p.stem)
    agents = (p for p in root.glob("*/*/subagents/agent-*.jsonl") if "." not in p.stem + p.parent.parent.name)
    return [*sorted(mains), *sorted(agents)]


def _modified_since(path: Path, since: datetime) -> bool:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC) >= since
    except OSError:
        return False


def _at_or_after(ts: str, since: datetime) -> bool:
    """A timestamp with no zone is read as UTC; one that does not parse is outside the window."""
    try:
        stamp = datetime.fromisoformat(ts)
    except ValueError:
        return False
    return stamp.replace(tzinfo=stamp.tzinfo or UTC) >= since


def _stat_since(path: Path, since: datetime) -> tuple[int, int] | None:
    """(size, mtime_ns) for a transcript modified at or after `since`; None when older or unreadable."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns) if datetime.fromtimestamp(st.st_mtime, tz=UTC) >= since else None


def _read_from(path: Path, offset: int) -> bytes:
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            return handle.read()
    except OSError:
        return b""


def read_session_turns_with_cursor(
    projects_dir: Path,
    host: str,
    chair_session: str,
    since: datetime,
    cursor: CursorState | None,
    *,
    prices: Mapping[str, Mapping[str, float]] | None = None,
) -> tuple[list[dict], CursorState]:
    """`read_session_turns` plus the cursor to pass next call; a cursor built for other host, chair or prices is ignored."""
    price_table = prices or {}
    scope = repr((host, chair_session, sorted((model, sorted(p.items())) for model, p in price_table.items())))
    base = cursor if cursor is not None and cursor.scope == scope else CursorState(scope=scope)
    stats = {p: st for p in _transcripts(Path(projects_dir)) if (st := _stat_since(p, since)) is not None}
    state = advance(
        base,
        stats,
        _read_from,
        lambda path, lines: keyed_turns_from_lines(lines, host, *_identity(path, chair_session), price_table),
    )
    turns = [turn for _, entry in state.paths for _, turn in entry.turns if _at_or_after(turn["ts"], since)]
    return sorted(turns, key=lambda turn: turn["ts"]), state


def read_session_turns(
    projects_dir: Path,
    host: str,
    chair_session: str,
    since: datetime,
    *,
    prices: Mapping[str, Mapping[str, float]] | None = None,
    cursor: CursorState | None = None,
) -> list[dict]:
    """Turns at or after `since` (timezone-aware), oldest first, from transcripts modified since; `chair_session` names kind "chair".

    With a `cursor`, only bytes past its offsets are read; `read_session_turns_with_cursor` returns the next cursor.
    """
    if cursor is not None:
        return read_session_turns_with_cursor(projects_dir, host, chair_session, since, cursor, prices=prices)[0]
    turns = [
        turn
        for path in _transcripts(Path(projects_dir))
        if _modified_since(path, since)
        for turn in turns_from_lines(_read_lines(path), host, *_identity(path, chair_session), prices or {})
        if _at_or_after(turn["ts"], since)
    ]
    return sorted(turns, key=lambda turn: turn["ts"])
