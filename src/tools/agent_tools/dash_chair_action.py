"""The action the chair is performing right now, read from the chair action log.

chair_exec.perform calls `deps.record` only after an action finishes, and every line it writes carries a
`status`. It writes no start record and closes none, so against today's log this reader always returns None.
The reader treats only an open record as current: a line with kind, initiative and phase and no `status`.
A later line with a `status` and the same kind, initiative and phase closes it. `since` is the line's `ts`.
"""

from __future__ import annotations

from pathlib import Path

from agent_tools.chair_read_record import ACTION_LOG
from agent_tools.jsonl_log import parse_log

Record = dict[str, object]


def _key(record: Record) -> tuple[object, object, object]:
    return record.get("kind"), record.get("initiative"), record.get("phase")


def _is_open(record: Record) -> bool:
    kind, initiative, phase = _key(record)
    return "status" not in record and bool(kind) and bool(initiative) and bool(phase)


def current_action(records: list[Record]) -> dict[str, str] | None:
    """The newest open record as {"kind", "target": "<initiative>/<phase>", "since"}; None when every record is closed."""
    opened: dict[tuple[object, object, object], Record] = {}
    for record in records:
        if _is_open(record):
            opened[_key(record)] = record
        elif "status" in record:
            opened.pop(_key(record), None)
    if not opened:
        return None
    last = list(opened.values())[-1]
    return {
        "kind": str(last["kind"]),
        "target": f"{last['initiative']}/{last['phase']}",
        "since": str(last.get("ts", "")),
    }


def read_current_action(runs_dir: Path) -> dict[str, str] | None:
    """Edge. `current_action` over runs_dir / ACTION_LOG; None when the log is missing. NULs are stripped and damaged lines skipped."""
    try:
        data = (runs_dir / ACTION_LOG).read_bytes()
    except FileNotFoundError:
        return None
    return current_action(list(parse_log(data.decode("utf-8", errors="replace")).rows))
