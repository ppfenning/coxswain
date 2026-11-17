"""The bounded runner: a fake runner and literals drive the core, one call per rule."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_tools import bounded_run
from agent_tools.bounded_run import (
    GH_TIMEOUT_SECONDS,
    GIT_TIMEOUT_SECONDS,
    BoundedResult,
    failure_line,
    popen_runner,
    run,
)


def fake(returncode: int, output: str):
    def runner(argv, timeout, cwd):
        return returncode, output

    return runner


def hung(argv, timeout, cwd):
    raise subprocess.TimeoutExpired(argv, timeout)


def missing(argv, timeout, cwd):
    raise FileNotFoundError(2, "No such file or directory", argv[0])


def test_result_holds_its_three_fields() -> None:
    result = BoundedResult(1, "boom", False)
    assert (result.returncode, result.output, result.timed_out) == (1, "boom", False)


def test_default_timeouts_are_positive_ints() -> None:
    assert isinstance(GH_TIMEOUT_SECONDS, int) and GH_TIMEOUT_SECONDS > 0
    assert isinstance(GIT_TIMEOUT_SECONDS, int) and GIT_TIMEOUT_SECONDS > 0


def test_success_passes_through() -> None:
    assert run(["git", "status"], 5, runner=fake(0, "ok")) == BoundedResult(0, "ok", False)


def test_nonzero_exit_passes_through() -> None:
    assert run(["git", "status"], 5, runner=fake(2, "bad")) == BoundedResult(2, "bad", False)


def test_runner_receives_argv_timeout_and_cwd(tmp_path: Path) -> None:
    seen = []

    def spy(argv, timeout, cwd):
        seen.append((list(argv), timeout, cwd))
        return 0, ""

    run(["git", "fetch"], 7, cwd=tmp_path, runner=spy)
    assert seen == [(["git", "fetch"], 7, tmp_path)]


def test_hung_runner_becomes_timed_out_result_with_failure_line() -> None:
    argv = ["gh", "pr", "view", "12"]
    result = run(argv, 30, runner=hung)
    assert result == BoundedResult(124, "", True)
    assert failure_line(argv, result, 30) == "gh pr timed out after 30 seconds"


def test_timeout_keeps_partial_output_from_bytes() -> None:
    def partial(argv, timeout, cwd):
        raise subprocess.TimeoutExpired(argv, timeout, output=b"half")

    assert run(["git", "log"], 5, runner=partial) == BoundedResult(124, "half", True)


def test_unrunnable_binary_becomes_127() -> None:
    argv = ["no-such-tool", "--version"]
    result = run(argv, 5, runner=missing)
    assert result.returncode == 127
    assert result.timed_out is False
    assert failure_line(argv, result, 5) == (
        "no-such-tool --version exited with code 127: [Errno 2] No such file or directory: 'no-such-tool'"
    )


def test_failure_line_exit_code_with_first_output_line() -> None:
    result = BoundedResult(1, "fatal: not a git repository\nmore\n", False)
    assert (
        failure_line(["git", "status", "-s"], result, 60)
        == "git status exited with code 1: fatal: not a git repository"
    )


def test_failure_line_skips_leading_blank_lines() -> None:
    assert (
        failure_line(["gh", "api"], BoundedResult(4, "\n\nHTTP 404\n", False), 30)
        == "gh api exited with code 4: HTTP 404"
    )


def test_failure_line_empty_output_omits_suffix() -> None:
    assert failure_line(["git", "push"], BoundedResult(128, "", False), 60) == "git push exited with code 128"


def test_failure_line_single_word_argv() -> None:
    assert failure_line(["git"], BoundedResult(1, "usage", False), 60) == "git exited with code 1: usage"


def test_failure_line_fractional_timeout() -> None:
    assert failure_line(["git", "gc"], BoundedResult(124, "", True), 2.5) == "git gc timed out after 2.5 seconds"


def test_popen_runner_returns_exit_code_and_merged_output(tmp_path: Path) -> None:
    code = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
    returncode, output = popen_runner([sys.executable, "-c", code], 20, tmp_path)
    assert returncode == 3
    assert sorted(output.split()) == ["err", "out"]


# The child prints "half", starts a heartbeat grandchild, then hangs. argv[1] is the heartbeat file, argv[2] the pid
# file, argv[3] "escape" to put the grandchild in its own session so a group kill cannot reach it.
HUNG_TREE = """
import subprocess, sys, time
beat = "import time\\nwhile True:\\n    open(%r, 'a').write('x')\\n    time.sleep(0.05)" % sys.argv[1]
grandchild = subprocess.Popen([sys.executable, "-c", beat], start_new_session=sys.argv[3] == "escape")
open(sys.argv[2], "w").write(str(grandchild.pid))
print("half", flush=True)
time.sleep(30)
"""


def hung_tree(tmp_path: Path, mode: str) -> list[str]:
    return [sys.executable, "-c", HUNG_TREE, str(tmp_path / "beat"), str(tmp_path / "pid"), mode]


def test_popen_runner_kills_the_whole_process_group_on_timeout(tmp_path: Path) -> None:
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        popen_runner(hung_tree(tmp_path, "same-group"), 2, tmp_path)
    assert caught.value.output.strip() == "half"
    beat = tmp_path / "beat"
    first = beat.stat().st_size
    time.sleep(0.5)
    assert (first > 0, beat.stat().st_size == first) == (True, True)


def test_popen_runner_gives_up_on_a_descendant_that_escaped_the_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bounded_run, "REAP_SECONDS", 0.3)
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            popen_runner(hung_tree(tmp_path, "escape"), 2, tmp_path)
    finally:
        os.kill(int((tmp_path / "pid").read_text()), signal.SIGKILL)
    assert time.monotonic() - started < 10
    assert caught.value.output.strip() == "half"
