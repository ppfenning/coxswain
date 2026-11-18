"""The bounded runner: a fake runner and literals drive the core, one call per rule."""

from __future__ import annotations

import os
import shutil
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


def test_result_holds_its_three_fields() -> None:
    result = BoundedResult(1, "boom", False)
    assert (result.returncode, result.output, result.timed_out) == (1, "boom", False)


def test_default_timeouts_are_positive_ints() -> None:
    assert isinstance(GH_TIMEOUT_SECONDS, int) and GH_TIMEOUT_SECONDS > 0
    assert isinstance(GIT_TIMEOUT_SECONDS, int) and GIT_TIMEOUT_SECONDS > 0


def test_a_real_child_that_exits_passes_its_code_and_merged_output_through(tmp_path: Path) -> None:
    assert run(["sh", "-c", "echo ok; exit 2"], 5, cwd=tmp_path) == BoundedResult(2, "ok\n", False)


def test_a_real_hung_child_becomes_a_timed_out_result_keeping_what_it_printed() -> None:
    argv = ["sh", "-c", "echo half; exec sleep 30"]
    started = time.monotonic()
    result = run(argv, 1)
    assert time.monotonic() - started < 10
    assert result == BoundedResult(124, "half\n", True)
    assert failure_line(argv, result, 1) == "sh -c timed out after 1 seconds"


def test_a_binary_that_does_not_exist_becomes_127() -> None:
    argv = ["no-such-tool-h5", "--version"]
    result = run(argv, 5)
    assert (result.returncode, result.timed_out) == (127, False)
    assert failure_line(argv, result, 5) == (
        "no-such-tool-h5 --version exited with code 127: [Errno 2] No such file or directory: 'no-such-tool-h5'"
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


# The child starts a heartbeat grandchild and prints "half" once the grandchild has written its own pid, then hangs.
# $1 is the heartbeat file, $2 the pid file, $3 "escape" to start the grandchild under setsid, in its own session, so a
# group kill cannot reach it. The grandchild writes its pid from inside that session, so "half" means the tree is up.
# The tree is POSIX sh, not Python: it has to be up before the 2-second timeout fires, and on a loaded runner one
# interpreter start can take longer than that. The heartbeat ends on its own after 30 seconds, so a tree killed before
# it was up leaks nothing.
HUNG_TREE = """
beat='echo $$ > "$1"; i=0; while [ $i -lt 600 ]; do printf x >> "$0"; sleep 0.05; i=$((i+1)); done'
if [ "$3" = escape ]; then setsid sh -c "$beat" "$1" "$2" & else sh -c "$beat" "$1" "$2" & fi
while [ ! -s "$2" ]; do :; done
echo half
exec sleep 30
"""


def hung_tree(tmp_path: Path, mode: str) -> list[str]:
    return ["sh", "-c", HUNG_TREE, "sh", str(tmp_path / "beat"), str(tmp_path / "pid"), mode]


def test_popen_runner_kills_the_whole_process_group_on_timeout(tmp_path: Path) -> None:
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        popen_runner(hung_tree(tmp_path, "same-group"), 2, tmp_path)
    assert caught.value.output.strip() == "half"
    beat = tmp_path / "beat"
    first = beat.stat().st_size
    time.sleep(0.5)
    assert (first > 0, beat.stat().st_size == first) == (True, True)


@pytest.mark.skipif(shutil.which("setsid") is None, reason="needs util-linux setsid to escape the group")
def test_popen_runner_gives_up_on_a_descendant_that_escaped_the_group(tmp_path: Path) -> None:
    """The unreaped-child case: a real grandchild in its own session holds the pipe; the runner still returns within
    the timeout plus REAP_SECONDS for the read and REAP_SECONDS for the wait."""
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            popen_runner(hung_tree(tmp_path, "escape"), 2, tmp_path)
    finally:
        os.kill(int((tmp_path / "pid").read_text()), signal.SIGKILL)
    assert time.monotonic() - started < 2 + 2 * bounded_run.REAP_SECONDS + 5
    assert caught.value.output.strip() == "half"
