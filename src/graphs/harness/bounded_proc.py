"""The harness's name for `core.bounded_proc`, the one bounded `subprocess.run` drop-in both packages share."""

from __future__ import annotations

from core.bounded_proc import (
    GH_TIMEOUT_SECONDS,
    GIT_SLOW_SUBCOMMANDS,
    GIT_SLOW_TIMEOUT_SECONDS,
    GIT_TIMEOUT_SECONDS,
    STEP_TIMEOUT_SECONDS,
    TIMEOUT_RETURNCODE,
    completed,
    default_timeout,
    git_subcommand,
    timeout_line,
)

__all__ = [
    "GH_TIMEOUT_SECONDS",
    "GIT_SLOW_SUBCOMMANDS",
    "GIT_SLOW_TIMEOUT_SECONDS",
    "GIT_TIMEOUT_SECONDS",
    "STEP_TIMEOUT_SECONDS",
    "TIMEOUT_RETURNCODE",
    "completed",
    "default_timeout",
    "git_subcommand",
    "timeout_line",
]
