"""The harness's bounded subprocess drop-in, against real child processes; no fakes."""

from __future__ import annotations

import subprocess
import time

import pytest

from harness import bounded_proc


def test_a_real_hung_child_is_exit_124_with_one_line_naming_it() -> None:
    started = time.monotonic()
    done = bounded_proc.completed(["sleep", "30"], 0.5, capture_output=True, text=True)
    assert time.monotonic() - started < 10
    assert (done.returncode, done.stdout, done.stderr) == (124, "", "sleep 30 timed out after 0.5 seconds")


def test_a_hung_child_under_check_raises_the_called_process_error_the_caller_already_handles() -> None:
    with pytest.raises(subprocess.CalledProcessError) as raised:
        bounded_proc.completed(["sleep", "30"], 0.5, capture_output=True, text=True, check=True)
    assert raised.value.returncode == 124


def test_a_child_that_finishes_is_returned_as_subprocess_run_returns_it() -> None:
    done = bounded_proc.completed(["sh", "-c", "echo out; echo err >&2; exit 3"], capture_output=True, text=True)
    assert (done.returncode, done.stdout, done.stderr) == (3, "out\n", "err\n")


def test_default_bounds_and_the_timeout_line() -> None:
    assert bounded_proc.default_timeout(["gh", "pr", "create"]) == 120
    assert bounded_proc.default_timeout(["git", "-C", "/r", "status"]) == 60
    assert bounded_proc.default_timeout(["git", "-C", "/r", "worktree", "add"]) == 600
    assert bounded_proc.default_timeout(["uv", "sync"]) == bounded_proc.STEP_TIMEOUT_SECONDS
    assert bounded_proc.timeout_line(["gh", "pr", "create", "--title", "t"], 120) == "gh pr timed out after 120 seconds"
