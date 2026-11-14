"""The chair action log: one JSON line per action the executor only records."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from agent_tools.jsonl_log import append_line

ACTION_LOG = "chair.actions.jsonl"  # in the runs directory


def action_line(action: Mapping[str, Any], epoch: int, ts: str) -> str:
    """Pure: one JSON line {"ts", "epoch", **action}, keys sorted."""
    return json.dumps({"ts": ts, "epoch": epoch, **action}, sort_keys=True)


def record_action_argv(holder: str, line: str) -> list[str]:
    """Pure: the store_cli arguments, after the module, that record one action line."""
    return ["record-action", "--holder", holder, line]


def recorder(
    runs_dir: Path,
    epoch: Callable[[], int],
    now: Callable[[], str],
    store: Callable[[list[str]], tuple[int, str]] | None = None,
    holder: str = "",
) -> Callable[[Mapping[str, Any]], None]:
    """Edge: a `record(action)` that appends `action_line(action, epoch(), now())` plus a newline to runs_dir / ACTION_LOG.

    The append comes first and always. A `store` then records the same line; a failed write is one stderr line.
    """

    def record(action: Mapping[str, Any]) -> None:
        line = action_line(action, epoch(), now())
        append_line(runs_dir / ACTION_LOG, json.loads(line), sync=False)  # the log is not a record of truth: no fsync
        if store is None:
            return
        code, output = store(record_action_argv(holder, line))
        if code != 0:
            print(f"chair: action not recorded in the store: {' '.join(output.split())[:120]}", file=sys.stderr)

    return record
