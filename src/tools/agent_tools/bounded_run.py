"""Run an argv under a timeout and return plain data, with a one-line failure message."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

GH_TIMEOUT_SECONDS = 30
GIT_TIMEOUT_SECONDS = 60

TIMEOUT_RETURNCODE = 124
UNRUNNABLE_RETURNCODE = 127

# After the kill, how long to keep reading the pipe and then waiting for the child before giving up on both.
REAP_SECONDS = 2

# A runner takes (argv, timeout, cwd) and returns (returncode, output); it raises TimeoutExpired or OSError.
Runner = Callable[[Sequence[str], float, Path | None], tuple[int, str]]


@dataclass(frozen=True)
class BoundedResult:
    """Exit code, merged stdout and stderr, and whether the timeout fired."""

    returncode: int
    output: str
    timed_out: bool


def _text(partial: str | bytes | None) -> str:
    """Partial output from a timeout, which subprocess may hand back as bytes or None."""
    if partial is None:
        return ""
    if isinstance(partial, bytes):
        return partial.decode("utf-8", errors="replace")
    return partial


def _kill_and_collect(proc: subprocess.Popen[str]) -> str:
    """Kill the child's process group, then read what it wrote without waiting on a descendant forever."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        proc.kill()
    try:
        output, _ = proc.communicate(timeout=REAP_SECONDS)
    except subprocess.TimeoutExpired as late:
        # A descendant that left the group (setsid, a daemonised helper) still holds the pipe: stop reading it.
        output = _text(late.output)
        if proc.stdout is not None:
            proc.stdout.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=REAP_SECONDS)
    return _text(output)


def popen_runner(argv: Sequence[str], timeout: float, cwd: Path | None) -> tuple[int, str]:
    """The edge: run argv in its own session; on timeout kill the group and raise TimeoutExpired with its output."""
    proc = subprocess.Popen(
        list(argv),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        raise subprocess.TimeoutExpired(list(argv), timeout, output=_kill_and_collect(proc)) from None
    return proc.returncode, output or ""


def run(
    argv: Sequence[str],
    timeout: float,
    cwd: Path | None = None,
    runner: Runner = popen_runner,
) -> BoundedResult:
    """Run argv through runner; a timeout becomes 124 with timed_out, an unrunnable binary becomes 127."""
    try:
        returncode, output = runner(argv, timeout, cwd)
    except subprocess.TimeoutExpired as expired:
        return BoundedResult(TIMEOUT_RETURNCODE, _text(expired.output), True)
    except OSError as error:
        return BoundedResult(UNRUNNABLE_RETURNCODE, str(error), False)
    return BoundedResult(returncode, output, False)


def failure_line(argv: Sequence[str], result: BoundedResult, timeout: float) -> str:
    """The first two argv words, then the timeout or the exit code and the first output line."""
    words = " ".join(argv[:2])
    if result.timed_out:
        return f"{words} timed out after {timeout:g} seconds"
    first = next((line.strip() for line in result.output.splitlines() if line.strip()), "")
    head = f"{words} exited with code {result.returncode}"
    return f"{head}: {first}" if first else head
