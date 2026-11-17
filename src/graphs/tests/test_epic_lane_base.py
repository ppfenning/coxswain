"""A lane's checks see their phase branch as COX_LANE_BASE, a ref that resolves in the lane's own worktree.

The unwired check (.github/check_unwired.py) diffs from that base, so a task is judged on its own patch and not on
every earlier phase and merged sibling between it and main.
"""

from __future__ import annotations

import sys

from test_epic_driver import cart, drive, initiative, repo  # noqa: F401 -- cart and repo are fixtures

from harness.epic import LANE_BASE_VAR

PHASE_BRANCH = "epic/demo-initiative/p1-foundations"
PROBE = (
    "import os, subprocess, sys; base = os.environ.get('{var}', ''); "
    "ok = base == '{branch}' and subprocess.run(['git', 'rev-parse', '--verify', '-q', base + '^{{commit}}'], "
    "capture_output=True, timeout=30).returncode == 0; sys.exit(0 if ok else 1)"
)


def test_a_lanes_checks_get_the_phase_branch_as_their_base(repo, cart, tmp_path) -> None:  # noqa: F811
    cmd = f'{sys.executable} -c "{PROBE.format(var=LANE_BASE_VAR, branch=PHASE_BRANCH)}"'
    (repo / ".agent-checks").write_text(cmd + "\n", encoding="utf-8")
    result, _ = drive(repo, cart, tmp_path, work=initiative(two_phases=False))
    task = next(t for t in result["tasks"] if t["id"] == "t1-probe")
    row = next(row for row in task["evidence"] if row["check"] == f"checks:{cmd}")
    assert task["status"] == "built", task
    assert row["output"].startswith("pass"), row
