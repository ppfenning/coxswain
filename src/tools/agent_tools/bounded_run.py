"""Run an argv under a timeout and return plain data, with a one-line failure message.

Two edges share the same timeouts. `run` (through `popen_runner`) starts the argv in its own session and kills the
whole process group on a timeout. `completed` is a drop-in for `subprocess.run` on a `gh` or `git` argv: it passes a
default timeout and turns the timeout into exit 124 with the failure line, so a caller's existing nonzero-exit path
reports it instead of a traceback.

The unreaped-child case: after the kill, `popen_runner` reads the pipe and waits for the child for at most
REAP_SECONDS each. A descendant that left the group (setsid, a daemonised credential helper) keeps the pipe open and
is not killed; a child stuck in uninterruptible sleep is not reaped in time. Either way the caller gets its timeout
result on time and the process is left behind, unwaited, until it exits on its own or this process does. That is
the price of never blocking past the bound."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

# One value for every `gh` call in both packages; harness/bounded_proc.py carries the same number, since the graphs
# package does not import this one, and a test holds the two equal.
GH_TIMEOUT_SECONDS = 120
GIT_TIMEOUT_SECONDS = 60
# git subcommands that talk to a remote or run repository hooks, so may legitimately take minutes.
GIT_SLOW_TIMEOUT_SECONDS = 600
GIT_SLOW_SUBCOMMANDS = frozenset({"fetch", "push", "pull", "clone", "ls-remote", "commit", "merge", "cherry-pick",
                                  "rebase", "am", "submodule", "worktree"})

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


def git_subcommand(argv: Sequence[str]) -> str:
    """The git subcommand after any `-C path`, `-c name=value` and other leading options; "" when there is none."""
    rest = list(argv[1:])
    while rest and rest[0].startswith("-"):
        rest = rest[2:] if rest[0] in ("-C", "-c") else rest[1:]
    return rest[0] if rest else ""


# An operator (or a smoke test) may shorten or lengthen the gh bound for one process tree; anything not a positive
# number is ignored.
GH_TIMEOUT_ENV = "COXSWAIN_GH_TIMEOUT_SECONDS"


def default_timeout(argv: Sequence[str], env: Mapping[str, str] | None = None) -> float:
    """GH_TIMEOUT_SECONDS (or a positive GH_TIMEOUT_ENV in `env`) for gh; for git, the slow bound for a remote or
    hook-running subcommand, else the short one; the slow bound for anything else."""
    program = os.path.basename(str(argv[0])) if argv else ""
    if program == "gh":
        raw = (env or {}).get(GH_TIMEOUT_ENV, "")
        try:
            override = float(raw)
        except ValueError:
            override = 0.0
        return override if override > 0 else GH_TIMEOUT_SECONDS
    if program == "git" and git_subcommand(argv) not in GIT_SLOW_SUBCOMMANDS:
        return GIT_TIMEOUT_SECONDS
    return GIT_SLOW_TIMEOUT_SECONDS


def _is_text(kwargs: dict) -> bool:
    return bool(kwargs.get("text") or kwargs.get("universal_newlines") or "encoding" in kwargs or "errors" in kwargs)


def completed(argv: Sequence[str], timeout: float | None = None, **kwargs) -> subprocess.CompletedProcess:
    """Edge. `subprocess.run(argv, timeout=..., **kwargs)` with `default_timeout(argv)` when none is given. A timeout
    returns exit 124 (raises CalledProcessError under `check=True`) with the failure line as stderr, or appended to
    stdout when stderr is merged into it; when stderr is not captured the line is printed to this process's stderr."""
    limit = default_timeout(argv, os.environ) if timeout is None else timeout
    try:
        return subprocess.run(list(argv), timeout=limit, **kwargs)
    except subprocess.TimeoutExpired as expired:
        line = failure_line(list(argv), BoundedResult(TIMEOUT_RETURNCODE, "", True), limit)
        text = _is_text(kwargs)
        stdout_piped = bool(kwargs.get("capture_output")) or kwargs.get("stdout") is subprocess.PIPE
        stderr_piped = bool(kwargs.get("capture_output")) or kwargs.get("stderr") is subprocess.PIPE
        merged = kwargs.get("stderr") is subprocess.STDOUT
        partial = _text(expired.output) if text else (expired.output or b"")
        encoded = line if text else line.encode()
        suffix = f"\n{line}" if merged else ""
        out = partial + (suffix if text else suffix.encode()) if stdout_piped else None
        err = encoded if stderr_piped else None
        if not stderr_piped and not (merged and stdout_piped):
            print(line, file=sys.stderr)
        if kwargs.get("check"):
            raise subprocess.CalledProcessError(TIMEOUT_RETURNCODE, list(argv), out, err) from None
        return subprocess.CompletedProcess(list(argv), TIMEOUT_RETURNCODE, out, err)
