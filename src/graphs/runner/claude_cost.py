"""What a Claude Code call cost and what its trace shows: session-total pricing, the commands a trace proves ran,
the call summary, the one row shape a call is recorded in, and the cost bounds a call is held to.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast


def next_spent(reported_usd: float) -> float:
    """The CLI's `total_cost_usd` is the session's running total, so it replaces the spend and never adds."""
    return reported_usd


def call_cost(previous_reported: float, reported: float, resumed: bool) -> float:
    """A resumed call's own cost is the rise in the session total; a fall means the session restarted."""
    return reported if not resumed or reported < previous_reported else reported - previous_reported


def thread_priced(previous_reported: float, reported: Any, resumed: bool) -> tuple[dict[str, float], float]:
    """A threaded call's `cost_usd` override and the session's new last total; a missing total overrides nothing."""
    return (
        ({}, previous_reported)
        if reported is None
        else ({"cost_usd": call_cost(previous_reported, float(reported), resumed)}, float(reported))
    )


def _tool_result_text(content: Any) -> str:
    """A tool result's own text, however the CLI shaped it."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text") or "") for block in content if isinstance(block, Mapping) and block.get("type") == "text"
        )
    return ""


def trace_commands(trace: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Every Bash call the trace shows was run, paired with its own tool result, in order.

    Matched by `tool_use_id`, since parallel tool calls in one assistant turn
    do not resolve in the order they were issued. Pure: reads `trace`, never
    mutates it, touches nothing on disk.
    """
    pending: dict[str, str] = {}
    commands: list[dict[str, str]] = []
    for event in trace:
        if not isinstance(event, Mapping):
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if event.get("type") == "assistant":
            for block in content or []:
                if isinstance(block, Mapping) and block.get("type") == "tool_use" and block.get("name") == "Bash":
                    tool_input = block.get("input")
                    command = tool_input.get("command") if isinstance(tool_input, Mapping) else None
                    tool_id = block.get("id")
                    if command and tool_id:
                        pending[tool_id] = str(command)
        elif event.get("type") == "user":
            for block in content or []:
                if isinstance(block, Mapping) and block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                    commands.append({
                        "command": pending.pop(block["tool_use_id"]),
                        "output": _tool_result_text(block.get("content")),
                        "source": "trace",
                    })
    return commands


_DENIED_TEXT_LIMIT = 300
_TARGET_KEYS = ("file_path", "path", "pattern", "url", "command")


def _content_blocks(events: Sequence[Mapping[str, Any]], event_type: str, block_type: str) -> list[Mapping[str, Any]]:
    """Content blocks of `block_type` from events of `event_type`, in trace order."""
    return [
        block
        for event in events
        if isinstance(event, Mapping) and event.get("type") == event_type and isinstance(event.get("message"), Mapping)
        for block in event["message"].get("content") or []
        if isinstance(block, Mapping) and block.get("type") == block_type
    ]


def _call_target(name: Any, tool_input: Any) -> str:
    """What a call acted on: the command for Bash, else the first identifying input field, else ''."""
    fields = tool_input if isinstance(tool_input, Mapping) else {}
    keys = ("command",) if name == "Bash" else _TARGET_KEYS
    return next((str(fields[key]) for key in keys if fields.get(key)), "")[:_DENIED_TEXT_LIMIT]


def _denied_calls(events: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Calls whose tool result is an error not starting "Exit code " (that is a Bash command that ran and failed), in call order.

    Paired by `tool_use_id`. Pure: reads `events`, never mutates them.
    """
    denials = {
        block.get("tool_use_id"): text
        for block in _content_blocks(events, "user", "tool_result")
        if block.get("is_error") is True
        for text in [_tool_result_text(block.get("content"))]
        if not text.startswith("Exit code ")
    }
    return [
        {
            "tool": str(block.get("name")),
            "target": _call_target(block.get("name"), block.get("input")),
            "message": denials[block["id"]][:_DENIED_TEXT_LIMIT],
        }
        for block in _content_blocks(events, "assistant", "tool_use")
        if block.get("id") in denials
    ]


def call_summary(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Tool counts, the ten most-read basenames, whole-file reads (a Read with no `limit`), the result's subtype and error flag, and the denied calls."""
    blocks = [
        block
        for event in events
        if isinstance(event, Mapping) and event.get("type") == "assistant" and isinstance(event.get("message"), Mapping)
        for block in event["message"].get("content") or []
        if isinstance(block, Mapping) and block.get("type") == "tool_use"
    ]
    reads = [
        block["input"] if isinstance(block.get("input"), Mapping) else {}
        for block in blocks
        if block.get("name") == "Read"
    ]
    result = next((e for e in events if isinstance(e, Mapping) and e.get("type") == "result"), {})
    return {
        "tool_uses": dict(Counter(str(block.get("name")) for block in blocks)),
        "reads": dict(Counter(str(r.get("file_path") or "").rsplit("/", 1)[-1] for r in reads).most_common(10)),
        "whole_file_reads": sum(1 for r in reads if r.get("limit") is None),
        "result": result.get("subtype"),
        "is_error": bool(result.get("is_error")),
        "denied": _denied_calls(events),
    }


def self_reported_commands(data: Mapping[str, Any]) -> list[dict[str, str]]:
    """The model's own account of what it ran — kept only when no trace exists to derive it."""
    reported = data.get("commands_run")
    if not isinstance(reported, list):
        return []
    return [{"command": str(item), "source": "self_report"} for item in reported if item]


def _trace_path(trace_dir: Path, role: str, n: int) -> Path:
    return trace_dir / f"{role}-{n}.jsonl"


def _call_fields(
    role: str,
    tier: str,
    model: str,
    tools: Sequence[str],
    payload: Mapping[str, Any],
    task_id: str | None = None,
    *,
    ceiling_usd: float | None = None,
    ceiling_source: str = "profile",
    hard_ceiling_usd: float | None = None,
    checkpoint_fractions: tuple[float, ...] | None = None,
    checkpoint_source: str = "default",
) -> dict[str, Any]:
    """The one shape a call is recorded in — success or failure alike."""
    usage: Mapping[str, Any] = cast("Mapping[str, Any]", payload.get("usage")) if isinstance(payload.get("usage"), Mapping) else {}
    return {
        "role": role,
        "task_id": task_id,
        "tier": tier,
        "model": model,
        "tools": list(tools),
        "cost_usd": payload.get("total_cost_usd"),
        "ceiling_usd": ceiling_usd,
        "ceiling_source": ceiling_source,
        **({"hard_ceiling_usd": hard_ceiling_usd} if hard_ceiling_usd is not None else {}),
        "checkpoint_fractions": list(checkpoint_fractions) if checkpoint_fractions else None,
        "checkpoint_source": checkpoint_source,
        "turns": payload.get("num_turns"),
        "duration_ms": payload.get("duration_ms"),
        # Split, not summed: a cache read costs a tenth of a fresh token,
        # and "4.6M input" meant nothing until the price revealed that most
        # of it was cached. Now the record says so itself.
        "input_tokens": int(usage.get("input_tokens") or 0),
        "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "cache_creation_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        "input_total": sum(
            int(usage.get(k) or 0)
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        ),
        "output_tokens": int(usage.get("output_tokens") or 0),
        **({"trace": payload["trace"]} if payload.get("trace") else {}),
        **({"commands_run": payload["commands_run"]} if "commands_run" in payload else {}),
        **({"summary": payload["summary"]} if "summary" in payload else {}),
    }


def _load_bounds(path: Path) -> dict[tuple[str, str], Mapping[str, Any]] | None:
    """Rows from `cox stats bounds --write`, keyed by (role, model). None when unreadable."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: cost bounds file unreadable ({path}): {exc}", file=sys.stderr)
        return None
    rows = raw.get("rows") if isinstance(raw, Mapping) else None
    if not isinstance(rows, list):
        return {}
    return {(str(r.get("role")), str(r.get("model"))): r for r in rows if isinstance(r, Mapping)}


def _effective_limit(shape_ceiling: float | None, node_cap: float | None) -> tuple[float | None, bool]:
    """min(shape, cap) per cost-bounds.md §6 rule 2. True when the cap is the binding number."""
    if node_cap is None:
        return shape_ceiling, False
    if shape_ceiling is None or node_cap < shape_ceiling:
        return node_cap, True
    return shape_ceiling, False


def _checkpoint_target(fractions: tuple[float, ...] | None, index: int, guide: float | None) -> float | None:
    """fractions[index] times the guide, as an absolute session total; None with no fractions left or no guide."""
    return fractions[index] * guide if fractions and guide is not None and index < len(fractions) else None
