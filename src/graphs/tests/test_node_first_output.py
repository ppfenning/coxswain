"""A model call that never starts fails its node quickly, and says why (E1, 2026-11-21: a `claude` wrapper that never
returned held two lanes silent for an hour). A streamed call prints claude's init event as soon as the CLI is up; one
that prints nothing within `first_output_s` fails with that reason, in its error and its ledger row."""

from __future__ import annotations

import json
import stat
import threading
import time
from pathlib import Path

import pytest

from runner import node_timeout
from runner.claude_code_runner import ClaudeCodeRunner, _NodeTimeout

PROFILE = {
    "profile": "fake-claude-code",
    "runner": "claude-code",
    "tiers": {"cheap": "haiku", "standard": "sonnet", "deep": "opus"},
    "tools": {"build": ["Read"]},
}
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
INIT = json.dumps({"type": "system", "subtype": "init"})


def _claude(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "claude"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_the_bound_is_the_profile_s_else_ten_minutes_and_zero_turns_it_off():
    assert node_timeout.first_output_s(None) == 600.0
    assert node_timeout.first_output_s(45) == 45.0
    assert node_timeout.first_output_s(0) == 0.0
    for bad in (-1, "10m", True):
        with pytest.raises(ValueError, match="first_output_s is a number of seconds"):
            node_timeout.first_output_s(bad)


def test_only_a_streamed_call_has_a_first_output_bound_and_never_one_past_its_whole_timeout():
    assert node_timeout.first_output_bound(600.0, streamed=True, timeout=1800) == 600.0
    assert node_timeout.first_output_bound(600.0, streamed=False, timeout=1800) is None
    assert node_timeout.first_output_bound(0.0, streamed=True, timeout=1800) is None
    assert node_timeout.first_output_bound(600.0, streamed=True, timeout=300) is None


def test_waiting_for_the_first_output_ends_on_the_output_on_the_exit_or_on_the_bound():
    seen = threading.Event()
    threading.Timer(0.2, seen.set).start()
    assert node_timeout.started(seen, lambda: None, 5.0)
    assert node_timeout.started(threading.Event(), lambda: 0, 5.0)  # exited: the call's own result says the rest
    began = time.monotonic()
    assert not node_timeout.started(threading.Event(), lambda: None, 0.3)
    assert time.monotonic() - began < 2
    assert node_timeout.started(threading.Event(), lambda: None, None)


def test_a_streamed_call_that_prints_nothing_fails_within_its_bound_with_the_reason(tmp_path: Path):
    script = _claude(tmp_path, "sleep 600")
    trace = tmp_path / "trace"
    runner = ClaudeCodeRunner({**PROFILE, "first_output_s": 1}, claude_bin=str(script), cwd=tmp_path, timeout=60,
                              trace_dir=trace)
    began = time.monotonic()
    with pytest.raises(_NodeTimeout, match=r"produced no output within 1s .* the model call never started") as raised:
        runner.run(role="plan", schema=SCHEMA, prompt="go")
    assert time.monotonic() - began < 15
    assert raised.value.ledger_error == str(raised.value)


def test_a_call_that_printed_its_init_runs_on_to_its_whole_timeout(tmp_path: Path):
    script = _claude(tmp_path, f"echo '{INIT}'\nsleep 600")
    runner = ClaudeCodeRunner({**PROFILE, "first_output_s": 1}, claude_bin=str(script), cwd=tmp_path, timeout=3,
                              trace_dir=tmp_path / "trace")
    with pytest.raises(_NodeTimeout, match="did not finish within 3s"):
        runner.run(role="plan", schema=SCHEMA, prompt="go")


def test_the_whole_call_s_bound_counts_from_its_start_the_first_output_wait_included(tmp_path: Path):
    script = _claude(tmp_path, f"sleep 2\necho '{INIT}'\nsleep 600")
    runner = ClaudeCodeRunner({**PROFILE, "first_output_s": 3}, claude_bin=str(script), cwd=tmp_path, timeout=4,
                              trace_dir=tmp_path / "trace")
    began = time.monotonic()
    with pytest.raises(_NodeTimeout, match="did not finish within 4s"):
        runner.run(role="plan", schema=SCHEMA, prompt="go")
    assert time.monotonic() - began < 5.5  # 4 s and the reap, not 2 s of waiting and then 4 more
