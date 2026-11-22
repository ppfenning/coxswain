"""The bounds on one node's model call: the whole call (`ClaudeCodeRunner.timeout`) and its first output.

A streamed call (`--output-format stream-json`, whenever the runner has a trace dir: every lane's) prints its first
event, claude's `system`/`init`, as soon as the CLI is up, before any model request. A call that prints nothing for
`first_output_s` never started: a wrapper that never execs claude (a version-manager shim waiting on a prompt), a binary that
hangs on start. It fails then, with a reason that says so (`no_first_output`), rather than holding its node, and the
lane under it, for the whole call's timeout. A call that is not streamed prints nothing until it ends, so it has no
first-output bound. The bound is the provider profile's `first_output_s` (seconds; 0 turns it off), else
`DEFAULT_FIRST_OUTPUT_S`.

Pure, but for `started`, which waits on the caller's event and clock.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

from runner.protocol import RunnerError

__all__ = ["DEFAULT_FIRST_OUTPUT_S", "PROFILE_KEY", "NodeTimeout", "first_output_bound", "first_output_s",
           "no_first_output", "started", "trace_cost"]

PROFILE_KEY = "first_output_s"
DEFAULT_FIRST_OUTPUT_S = 600.0
_POLL_S = 0.1


class NodeTimeout(RunnerError):
    """A node's subprocess outlived one of its bounds. A RunnerError, so every existing handler still matches.
    `ledger_error` is what its ledger row says, when not the whole call's timeout."""

    def __init__(self, message: str, ledger_error: str = "") -> None:
        super().__init__(message)
        self.ledger_error = ledger_error


def first_output_s(value: object) -> float:
    """The profile's `first_output_s` as seconds: the default when absent, 0 for none. ValueError for anything that
    is not a number of seconds at or above zero."""
    if value is None:
        return DEFAULT_FIRST_OUTPUT_S
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise ValueError(f"{PROFILE_KEY} is a number of seconds, 0 for no bound; got {value!r}")
    return float(value)


def first_output_bound(seconds: float, streamed: bool, timeout: float | None) -> float | None:
    """The first-output bound a call runs under, None for none: only a streamed call has one, 0 turns it off, and one
    at or past the whole call's `timeout` adds nothing."""
    if not streamed or seconds <= 0 or (timeout is not None and seconds >= timeout):
        return None
    return seconds


def no_first_output(role: str, seconds: float, command: str) -> str:
    """Why a node failed when its call printed nothing within its bound."""
    return (f"node '{role}' produced no output within {seconds:g}s of starting `{command}`: the model call never "
            f"started; check that `{command}` runs claude on this machine")


def started(first: threading.Event, exited: Callable[[], object], seconds: float | None,
            clock: Callable[[], float] = time.monotonic) -> bool:
    """Edge. Whether the call printed its first output (`first` set) or exited (`exited()` not None) within `seconds`;
    True at once with no bound."""
    if seconds is None:
        return True
    deadline = clock() + seconds
    while not first.is_set() and exited() is None:
        left = deadline - clock()
        if left <= 0:
            return False
        first.wait(min(_POLL_S, left))
    return True


def trace_cost(path: Path) -> float | None:
    """`total_cost_usd` of the last result event in a trace file; None when there is no such file or event.

    Wrong belief: a timed-out node leaves a trace. `_payload` writes the file only after the CLI
    returns, so on a real timeout there is usually none and the row's cost stays null.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    costs: list[float] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result" and event.get("total_cost_usd") is not None:
            costs.append(float(event["total_cost_usd"]))
    return costs[-1] if costs else None
