"""The `lane-env` check writes both lines under this host's own /bin/sh and userland, the way the harness runs it
(`shell=True`). macOS's BSD `printenv` prints only its first argument, so `printenv A B` left the trace dir alone in
the file and the launch test could not read PATH from it."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from smoke import plan


def _lane_env(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    out = tmp_path / "lane-env.txt"
    (check,) = [c for c in plan.smoke_checks(str(out)) if c["name"] == "lane-env"]
    return subprocess.run(check["cmd"], shell=True, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)


def test_the_lane_env_check_writes_the_trace_dir_and_path_on_this_host(tmp_path: Path) -> None:
    path = f"/r/.venv/bin:/i/bin:{os.environ['PATH']}"
    done = _lane_env(tmp_path, {"PATH": path, "AGENT_GRAPHS_TRACE_DIR": "/runs/ship-1-trace"})
    assert done.returncode == 0, done.stderr
    text = (tmp_path / "lane-env.txt").read_text(encoding="utf-8")
    assert plan.lane_env_lines(text) == ("/runs/ship-1-trace", path), text


def test_the_lane_env_check_fails_when_the_trace_dir_is_unset(tmp_path: Path) -> None:
    assert _lane_env(tmp_path, {"PATH": os.environ["PATH"]}).returncode != 0
    assert _lane_env(tmp_path, {"PATH": os.environ["PATH"], "AGENT_GRAPHS_TRACE_DIR": ""}).returncode != 0
