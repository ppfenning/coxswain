"""F7: the request bodies the Anthropic runner builds carry prompt-cache breakpoints where the rules put them."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from runner.anthropic_runner import _to_anthropic, node_request
from runner.pricing import price_call

EPHEMERAL = {"type": "ephemeral"}
TOOLS = [{"name": "read_file", "description": "d", "parameters": {"type": "object"}}]


def _breakpoints(node: Any, path: str = "") -> list[str]:
    """The path of every block in a request body that carries `cache_control`."""
    if isinstance(node, dict):
        here = [path] if "cache_control" in node else []
        return here + [p for k, v in node.items() for p in _breakpoints(v, f"{path}.{k}")]
    if isinstance(node, list):
        return [p for i, v in enumerate(node) for p in _breakpoints(v, f"{path}[{i}]")]
    return []


def _history(turns: int) -> list[dict[str, Any]]:
    """A tool_loop message list after `turns` tool calls, each answered."""
    messages: list[dict[str, Any]] = [{"role": "system", "content": "skill and packs"}, {"role": "user", "content": "go"}]
    for n in range(turns):
        messages.append({"role": "assistant", "tool_calls": [{"id": f"t{n}", "name": "read_file", "arguments": {}}]})
        messages.append({"role": "tool", "tool_call_id": f"t{n}", "content": f"out {n}", "is_error": False})
    return messages


def test_each_tool_loop_turn_marks_the_system_block_and_its_own_last_block_only():
    for turns in (0, 1, 3):
        history = _history(turns)
        before = copy.deepcopy(history)
        body = _to_anthropic(history, TOOLS)
        last = len(body["messages"]) - 1
        assert body["system"] == [{"type": "text", "text": "skill and packs", "cache_control": EPHEMERAL}]
        assert _breakpoints(body) == [".system[0]", f".messages[{last}].content[0]"]
        assert history == before  # the loop's own history never carries a marker into the next turn


def test_the_first_turn_prompt_string_becomes_a_marked_text_block():
    body = _to_anthropic(_history(0), TOOLS)
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "go", "cache_control": EPHEMERAL}]}]


def test_a_node_call_marks_the_system_block_and_not_its_unique_prompt():
    body = node_request("m", 100, "skill and packs", "the task", "high", {"type": "object"})
    assert body["system"] == [{"type": "text", "text": "skill and packs", "cache_control": EPHEMERAL}]
    assert body["messages"] == [{"role": "user", "content": "the task"}]
    assert _breakpoints(body) == [".system[0]"]
    assert node_request("m", 100, "", "the task", "high", {})["system"] is None


def test_cache_writes_and_reads_are_priced_beside_uncached_input():
    rates = {"m": {"input": 10.0, "output": 50.0}}
    assert price_call(rates, "m", 1_000_000, 0, 1_000_000, 1_000_000) == pytest.approx(10.0 + 12.5 + 1.0)
    quoted = {"m": {"input": 10.0, "output": 50.0, "cache_write": 20.0, "cache_read": 0.5}}
    assert price_call(quoted, "m", 0, 0, 1_000_000, 1_000_000) == pytest.approx(20.5)
    assert price_call(rates, "m", 1_000_000, 1_000_000) == pytest.approx(60.0)
    # the family read price when the profile quotes none: 0.05x on Opus 5.5, 0.025x on Fable 5.2
    assert price_call({"claude-opus-5-5": {"input": 4.0, "output": 20.0}}, "claude-opus-5-5", 0, 0, 0, 1_000_000) == pytest.approx(0.2)
    assert price_call({"claude-fable-5-2": {"input": 8.0, "output": 40.0}}, "claude-fable-5-2", 0, 0, 0, 1_000_000) == pytest.approx(0.2)
