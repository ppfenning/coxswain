"""A drop-in for `subprocess.run` on a `gh` or `git` argv: a default timeout, and a timeout that reads as exit 124
with a one-line reason instead of a traceback, so the caller's existing nonzero-exit path reports it.

The graphs package does not import the tools package, so this is the harness's twin of
`agent_tools.bounded_run.completed`; the tools suite holds the timeout numbers of the two equal."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from typing import Any

GH_TIMEOUT_SECONDS = 120
GIT_TIMEOUT_SECONDS = 60
GIT_SLOW_TIMEOUT_SECONDS = 600
# A step whose length scales with what it builds (a venv sync, a tool install, a generator): long, but never forever.
STEP_TIMEOUT_SECONDS = 1800
GIT_SLOW_SUBCOMMANDS = frozenset({"fetch", "push", "pull", "clone", "ls-remote", "commit", "merge", "cherry-pick",
                                  "rebase", "am", "submodule", "worktree"})
TIMEOUT_RETURNCODE = 124


def git_subcommand(argv: Sequence[str]) -> str:
    """The git subcommand after any `-C path`, `-c name=value` and other leading options; "" when there is none."""
    rest = list(argv[1:])
    while rest and rest[0].startswith("-"):
        rest = rest[2:] if rest[0] in ("-C", "-c") else rest[1:]
    return rest[0] if rest else ""


def default_timeout(argv: Sequence[str]) -> float:
    program = os.path.basename(str(argv[0])) if argv else ""
    if program == "gh":
        return GH_TIMEOUT_SECONDS
    if program == "git" and git_subcommand(argv) not in GIT_SLOW_SUBCOMMANDS:
        return GIT_TIMEOUT_SECONDS
    if program == "git":
        return GIT_SLOW_TIMEOUT_SECONDS
    return STEP_TIMEOUT_SECONDS


def timeout_line(argv: Sequence[str], timeout: float) -> str:
    return f"{' '.join(argv[:2])} timed out after {timeout:g} seconds"


def completed(argv: Sequence[str], timeout: float | None = None, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """Edge. `subprocess.run(argv, timeout=..., **kwargs)`; a timeout returns exit 124 with `timeout_line` as stderr,
    or appended to stdout when stderr is merged into it, and printed to this process's stderr when not captured."""
    limit = default_timeout(argv) if timeout is None else timeout
    try:
        return subprocess.run(list(argv), timeout=limit, **kwargs)
    except subprocess.TimeoutExpired as expired:
        line = timeout_line(argv, limit)
        text = bool(kwargs.get("text") or kwargs.get("universal_newlines") or "encoding" in kwargs or "errors" in kwargs)
        stdout_piped = bool(kwargs.get("capture_output")) or kwargs.get("stdout") is subprocess.PIPE
        stderr_piped = bool(kwargs.get("capture_output")) or kwargs.get("stderr") is subprocess.PIPE
        merged = kwargs.get("stderr") is subprocess.STDOUT
        raw = expired.output or (b"" if not text else "")
        partial = raw.decode("utf-8", errors="replace") if text and isinstance(raw, bytes) else raw
        suffix = f"\n{line}" if merged else ""
        out = partial + (suffix if text else suffix.encode()) if stdout_piped else None
        err = (line if text else line.encode()) if stderr_piped else None
        if not stderr_piped and not (merged and stdout_piped):
            print(line, file=sys.stderr)
        if kwargs.get("check"):
            raise subprocess.CalledProcessError(TIMEOUT_RETURNCODE, list(argv), out, err) from None
        return subprocess.CompletedProcess(list(argv), TIMEOUT_RETURNCODE, out, err)
