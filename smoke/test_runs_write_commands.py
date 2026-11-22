"""The `cox runs` and `cox chair` commands that write, each run through the installed `cox` against a fresh store and
a real clone: `runs land-batch`, `runs clean`, `runs stop`, `runs move` and `chair freeze-lands`.

Their unit tests replace git, the store and the process table with doubles, so a wrong argument passed from the
command to a correct core passes there (adversarial review F1). Each case here asserts the effect where it lands: a
branch gone, a process gone, a store row written.
"""

import json
import subprocess
import threading
from dataclasses import replace

import pytest

from smoke import plan

TASK = {"id": "t1", "title": "First task", "phase": "p1", "state": "approved", "repo": "app"}
RECORD = {"run": "ship-1", "task": "t1", "phase": "p1", "review": {"verdict": "approve"}}
SEED = """import json
from harness.store_migrate import open_store
from harness.store_write import Store
conn = open_store({url!r}, "2026-11-22T00:00:00Z")
try:
    Store(conn).record_run({{"run_id": "ship-1", "graph": "epic", "status": "running"}})
finally:
    conn.close()
print(json.dumps(True))
"""
PAUSED = """import json
from harness.store_migrate import open_store
from harness.store_pause import is_paused
conn = open_store({url!r}, "2026-11-22T00:00:00Z")
try:
    print(json.dumps(is_paused(conn, "ship-1")))
finally:
    conn.close()
"""
FROZEN = """import json
from datetime import UTC, datetime
from pathlib import Path
from agent_tools import chair_land_freeze
freeze = chair_land_freeze.read_freeze(Path({runs!r}), datetime.now(UTC))
print(json.dumps(None if freeze is None else freeze.reason))
"""


def _ok(done):
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def _live(sandbox, run_id):
    """A process standing in for a run's lane, its pid in `runs/<run_id>.pid` as a launch writes it. A thread reaps it,
    as the lane's parent would, so a stopped process does not linger as a zombie the liveness check still sees."""
    proc = subprocess.Popen(["sleep", "600"], start_new_session=True)
    sandbox.write(f"runs/{run_id}.pid", f"{proc.pid}\n")
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc


def test_runs_land_batch_lands_a_ready_phase_on_origins_main(launchable, repo):
    # An approved phase on its own branch, as a run leaves it, landed through the local forge the bare origin gets.
    sandbox = launchable.sandbox
    sandbox.git(repo.path, "checkout", "--quiet", "-b", "epic/ship/p1")
    (repo.path / "notes").mkdir()
    (repo.path / plan.SMOKE_NOTE).write_text("ok\n", encoding="utf-8")  # what the team cartridge's `note` check wants
    sandbox.git(repo.path, "add", plan.SMOKE_NOTE)
    sandbox.git(repo.path, "commit", "--quiet", "-m", "t1")
    sandbox.git(repo.path, "checkout", "--quiet", "main")
    sandbox.write("work/ship/initiative.md", plan.work_item_text({"title": "Ship", "repo": "~/src/app", "draft": "false"}, "Ship."))
    sandbox.write("work/ship/p1/t1.md", plan.work_item_text(TASK, "Task body."))
    sandbox.write("runs/ship-1/tasks/p1/t1.json", json.dumps(RECORD))
    sandbox.write("runs/ship-1:p1.json", json.dumps({"run_id": "ship-1:p1"}))
    _ok(sandbox.cox("route", "import"))

    planned = json.loads(_ok(sandbox.cox("runs", "land-batch", "--repo", "~/src/app")))
    assert planned["ready"] == ["ship/p1"], planned
    # The cartridge's `lane-env` check wants the trace dir a lane is given; a land started by hand passes it on.
    traced = replace(sandbox, env={**sandbox.env, "AGENT_GRAPHS_TRACE_DIR": str(sandbox.workspace / "runs" / "batch-trace")})
    out = _ok(traced.cox("runs", "land-batch", "--repo", "~/src/app", "--apply", "--wait-s", "0", timeout=300))
    assert "batch landed ship/p1" in out and "landed=1 returned=0" in out, out
    assert sandbox.git(repo.origin, "show", f"main:{plan.SMOKE_NOTE}") == "ok"
    assert sandbox.git(repo.origin, "branch", "--list", "epic/ship/p1") == ""
    assert json.loads((sandbox.workspace / "runs/ship-1/tasks/p1/t1.json").read_text(encoding="utf-8"))["landed"]


def test_runs_clean_deletes_a_landed_runs_branch_and_keeps_an_unlanded_one(sandbox, repo):
    sandbox.git(repo.path, "branch", "agents/r1/t1")
    sandbox.git(repo.path, "branch", "agents/r1/t2")
    sandbox.write("runs/r1/tasks/p1/t1.json", json.dumps({"task": "t1", "landed": True}))
    sandbox.write("runs/r1/tasks/p1/t2.json", json.dumps({"task": "t2"}))
    out = _ok(sandbox.cox("runs", "clean", "r1", "--repo", "~/src/app", "--apply"))
    assert "deleted branch agents/r1/t1" in out, out
    assert sandbox.git(repo.path, "branch", "--list", "agents/r1/*") == "agents/r1/t2"


@pytest.mark.xfail(strict=True, reason="cleanup.plan_cleanup dooms every epic/*--* branch, not only the run's own: "
                   "a landed t1 in r1 takes another initiative's private merge target for its t1 with it")
def test_runs_clean_leaves_another_initiatives_merge_target_alone(sandbox, repo):
    sandbox.git(repo.path, "branch", "agents/r1/t1")
    sandbox.git(repo.path, "branch", "epic/dock/p1--t1")
    sandbox.write("runs/r1/tasks/p1/t1.json", json.dumps({"task": "t1", "landed": True}))
    _ok(sandbox.cox("runs", "clean", "r1", "--repo", "~/src/app", "--apply"))
    assert sandbox.git(repo.path, "branch", "--list", "epic/dock/*") == "epic/dock/p1--t1"


def test_runs_stop_ends_a_local_runs_process(sandbox):
    proc = _live(sandbox, "r1")
    out = _ok(sandbox.cox("runs", "stop", "r1", "--runs-dir", str(sandbox.workspace / "runs"), timeout=90))
    assert "stopped r1" in out, out
    assert proc.poll() is not None


def test_runs_move_pauses_stops_and_clears_the_run_before_the_relaunch_refuses(launchable, repo, tmp_path):
    # lane-a is not a real host. Its relaunch refuses at the host's auth check, since the sandbox configures none; an
    # `ssh` that only fails sits first on PATH so that, should any step reach for the host, nothing leaves the machine.
    # The steps before it are real and must have landed: the store row paused, the process gone, the phase branch gone.
    sandbox = launchable.sandbox
    sandbox.git(repo.path, "branch", "epic/ship/p1")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text("#!/bin/sh\nexit 255\n", encoding="utf-8")
    (bin_dir / "ssh").chmod(0o755)
    profile = sandbox.home / ".config" / "agent-tools" / "profile.yaml"
    profile.write_text(profile.read_text(encoding="utf-8")
                       + "lane_hosts:\n  - name: lane-a\n    ssh: lane-a.invalid\n    workspace_dir: /srv/lane\n", encoding="utf-8")
    sandbox.write("work/ship/initiative.md", plan.work_item_text({"title": "Ship", "repo": "~/src/app", "draft": "false"}, "Ship."))
    assert sandbox.python_json(SEED.format(url=sandbox.store_url)) is True
    proc = _live(sandbox, "ship-1")
    shimmed = replace(sandbox, env={**sandbox.env, "PATH": f"{bin_dir}:{sandbox.env['PATH']}"})

    done = shimmed.cox("runs", "move", "ship-1", "--to", "lane-a", "--reason", "smoke",
                       "--runs-dir", str(sandbox.workspace / "runs"), "--json", timeout=120)
    assert done.returncode == 1, done.stdout + done.stderr
    report = json.loads(done.stdout)
    steps = [(s["step"]["kind"], s["ok"]) for s in report["move_plan"]]
    assert steps == [("pause", True), ("stop", True), ("clear_branches", True), ("relaunch", False)], report
    assert "lane-a" in report["reason"], report
    assert sandbox.python_json(PAUSED.format(url=sandbox.store_url)) is True
    assert proc.poll() is not None
    assert sandbox.git(repo.path, "branch", "--list", "epic/ship/*") == ""


def test_chair_freeze_lands_writes_the_freeze_to_the_store_and_lifts_it(sandbox):
    runs = str(sandbox.workspace / "runs")
    out = _ok(sandbox.cox("chair", "freeze-lands", "--for", "5", "--reason", "cut 2026.11"))
    assert "lands frozen until" in out and "cut 2026.11" in out, out
    assert sandbox.python_json(FROZEN.format(runs=runs)) == "cut 2026.11"
    assert "cut 2026.11" in _ok(sandbox.cox("chair", "freeze-lands"))
    assert "lands are not frozen" in _ok(sandbox.cox("chair", "freeze-lands", "--off"))
    assert sandbox.python_json(FROZEN.format(runs=runs)) is None
