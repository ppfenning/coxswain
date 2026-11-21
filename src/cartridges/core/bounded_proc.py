"""A drop-in for `subprocess.run` on a `gh` or `git` argv: a default timeout, and a timeout that reads as exit 124
with a one-line reason instead of a traceback, so the caller's existing nonzero-exit path reports it.

The one implementation for both packages. The graphs package does not import the tools package, and the tools
package keeps clear of `harness/__init__.py`'s imports, so this lives in `core`, which both already import:
`harness.bounded_proc` re-exports it, and `agent_tools.bounded_run` re-exports it beside its process-group runner."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import Any, cast

# One value for every `gh` call in both packages.
GH_TIMEOUT_SECONDS = 120
GIT_TIMEOUT_SECONDS = 60
# git subcommands that talk to a remote or run repository hooks, so may legitimately take minutes.
GIT_SLOW_TIMEOUT_SECONDS = 600
# A step whose length scales with what it builds (a venv sync, a tool install, a generator): long, but never forever.
STEP_TIMEOUT_SECONDS = 1800
GIT_SLOW_SUBCOMMANDS = frozenset({"fetch", "push", "pull", "clone", "ls-remote", "commit", "merge", "cherry-pick",
                                  "rebase", "am", "submodule", "worktree"})
TIMEOUT_RETURNCODE = 124
# An operator (or a smoke test) may shorten or lengthen the gh bound for one process tree; anything not a positive
# number is ignored.
GH_TIMEOUT_ENV = "COXSWAIN_GH_TIMEOUT_SECONDS"


def git_subcommand(argv: Sequence[str]) -> str:
    """The git subcommand after any `-C path`, `-c name=value` and other leading options; "" when there is none."""
    rest = list(argv[1:])
    while rest and rest[0].startswith("-"):
        rest = rest[2:] if rest[0] in ("-C", "-c") else rest[1:]
    return rest[0] if rest else ""


def default_timeout(argv: Sequence[str], env: Mapping[str, str] | None = None) -> float:
    """GH_TIMEOUT_SECONDS (or a positive GH_TIMEOUT_ENV in `env`) for gh; for git, the slow bound for a remote or
    hook-running subcommand, else the short one; the step bound for anything else."""
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
    if program == "git":
        return GIT_SLOW_TIMEOUT_SECONDS
    return STEP_TIMEOUT_SECONDS


def timeout_line(argv: Sequence[str], timeout: float) -> str:
    """The first two argv words and the bound that fired."""
    return f"{' '.join(argv[:2])} timed out after {timeout:g} seconds"


def _is_text(kwargs: Mapping[str, Any]) -> bool:
    return bool(kwargs.get("text") or kwargs.get("universal_newlines") or "encoding" in kwargs or "errors" in kwargs)


def _partial(raw: str | bytes | None, text: bool) -> str | bytes:
    """What a timed-out child had written, as text or bytes to match the call."""
    if not text:
        return raw.encode() if isinstance(raw, str) else raw or b""
    return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw or ""


def _words(argv: Sequence[str] | str) -> list[str]:
    """The argv as a list; a `shell=True` command string is split on whitespace, for the failure line only."""
    return argv.split() if isinstance(argv, str) else list(argv)


def completed(argv: Sequence[str] | str, timeout: float | None = None, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """Edge. `subprocess.run(argv, timeout=..., **kwargs)` with `default_timeout(argv)` when none is given. A timeout
    returns exit 124 (raises CalledProcessError under `check=True`) with `timeout_line` as stderr, or appended to
    stdout when stderr is merged into it; when stderr is not captured the line is printed to this process's stderr."""
    words = _words(argv)
    limit = default_timeout(words, os.environ) if timeout is None else timeout
    try:
        # cast: with **kwargs pyright cannot pick one of run's overloads, and the result is whatever the kwargs make it.
        return cast("subprocess.CompletedProcess[Any]", subprocess.run(argv if isinstance(argv, str) else words, timeout=limit, **kwargs))
    except subprocess.TimeoutExpired as expired:
        line = timeout_line(words, limit)
        text = _is_text(kwargs)
        stdout_piped = bool(kwargs.get("capture_output")) or kwargs.get("stdout") is subprocess.PIPE
        stderr_piped = bool(kwargs.get("capture_output")) or kwargs.get("stderr") is subprocess.PIPE
        merged = kwargs.get("stderr") is subprocess.STDOUT
        suffix = f"\n{line}" if merged else ""
        partial = _partial(expired.output, text)
        out = (partial + suffix if isinstance(partial, str) else partial + suffix.encode()) if stdout_piped else None
        err = (line if text else line.encode()) if stderr_piped else None
        if not stderr_piped and not (merged and stdout_piped):
            print(line, file=sys.stderr)
        if kwargs.get("check"):
            raise subprocess.CalledProcessError(TIMEOUT_RETURNCODE, argv, out, err) from None
        return subprocess.CompletedProcess(argv, TIMEOUT_RETURNCODE, out, err)
