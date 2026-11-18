"""The harness, started the way a person starts it: `cox route launch epic` -> `<harness_dir>/shell.py` ->
`harness.cli.main` -> `_run_graph` -> `run_epic`, detached, under the install's interpreter and the environment
cox builds for it (`route.child_env`), against real git and the test's Postgres.

Every node of a graph is a model call. The provider profile names the harness's own offline runner
(`runner: scripted`, `harness.runners._scripted`), which replays canned answers by role; nothing else is stood in for.
cox resolves the profile, places the lane, builds the argv and the child environment and starts the harness; the
harness resolves the team cartridge and its skills, applies the patch in a real worktree, runs the cartridge's check
there, gates, merges onto the phase branch, pushes and records the run in the store. The lifecycle test runs the
harness's entry point directly, with the `--scripted` flag, for the generic single-graph branch of `_run_graph`.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from smoke import plan

psycopg = pytest.importorskip("psycopg")

INITIATIVE, TASK, NOTE = "ship", "t1", plan.SMOKE_NOTE
EXIT_WAIT_S = 120
SUMMARY = "epic {run}: 1 phase(s) complete, 0 partial, 0 blocked, 0 awaiting land, 0 task(s) quarantined"


def _filed(sandbox) -> None:
    """Initiative `ship`, one ready task `t1` in phase p1, imported into the store as `cox route import` does."""
    sandbox.write(f"work/{INITIATIVE}/initiative.md",
                  plan.work_item_text({"id": INITIATIVE, "title": "Ship", "repo": "~/src/app"}, "Ship the note."))
    sandbox.write(f"work/{INITIATIVE}/p1/{TASK}.md", plan.work_item_text(
        {"id": TASK, "title": "Add the note", "state": "ready", "phase": "p1", "repo": "app"}, f"Add {NOTE} holding ok."))
    imported = sandbox.cox("route", "import")
    assert imported.returncode == 0, imported.stdout + imported.stderr


def _responses(launchable):
    return launchable.scripted(plan.scripted_epic_responses(plan.new_file_patch(NOTE, "ok"), [NOTE]))


def _run_row(sandbox, run_id: str) -> dict | None:
    with psycopg.connect(sandbox.store_url, connect_timeout=10) as conn:
        row = conn.execute("SELECT status, ended_at, cartridge_team FROM runs WHERE run_id = %s", (run_id,)).fetchone()
    return None if row is None else {"status": row[0], "ended_at": row[1], "team": row[2]}


def _registered_worktrees(sandbox, repo) -> list[str]:
    listing = sandbox.git(repo.path, "worktree", "list", "--porcelain")
    return [line.removeprefix("worktree ") for line in listing.splitlines() if line.startswith("worktree ")]


def _wait_exit(pid: int) -> bool:
    deadline = time.monotonic() + EXIT_WAIT_S
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.2)
    return False


def _launched(sandbox, run_id: str) -> str:
    """`cox route launch epic` from the workspace; waits for the detached harness to exit and returns its log."""
    launch = sandbox.cox("route", "launch", "epic", "--initiative", f"work/{INITIATIVE}", "--repo", "~/src/app",
                         cwd=sandbox.workspace)
    assert launch.returncode == 0, launch.stdout + launch.stderr
    assert f"run {run_id}" in launch.stdout.splitlines(), launch.stdout
    runs = sandbox.workspace / "runs"
    pid = int((runs / f"{run_id}.pid").read_text(encoding="utf-8"))
    assert json.loads((runs / f"{run_id}.launched.json").read_text(encoding="utf-8"))["initiative"] == INITIATIVE
    assert _wait_exit(pid), f"the harness (pid {pid}) was still running after {EXIT_WAIT_S}s"
    log = (runs / f"{run_id}.log").read_text(encoding="utf-8")
    assert log.startswith(f"graph started: {run_id}\n"), log[:2000]
    return log


def test_cox_route_launch_runs_an_epic_offline_and_its_phase_lands_on_main(launchable, repo):
    sandbox = launchable.sandbox
    _filed(sandbox)
    _responses(launchable)
    log = _launched(sandbox, "ship-1")
    # the check ran in the build worktree under the environment cox built for the harness: git, grep and a PATH
    assert SUMMARY.format(run="ship-1") in log, log[-3000:]

    # the phase branch carries the built file, on this clone and on origin; the build worktree is gone
    assert sandbox.git(repo.path, "show", f"epic/{INITIATIVE}/p1:{NOTE}") == "ok"
    assert sandbox.git(repo.origin, "rev-parse", f"epic/{INITIATIVE}/p1") == sandbox.git(repo.path, "rev-parse", f"epic/{INITIATIVE}/p1")
    assert _registered_worktrees(sandbox, repo) == [str(repo.path)]
    row = sandbox.store_row(INITIATIVE, TASK)
    assert (row["state"], row["updated_by"]) == ("approved", "epic-driver:ship-1")
    run = _run_row(sandbox, "ship-1")
    assert run is not None and (run["status"], run["team"]) == ("ok", "smoke") and run["ended_at"], run

    # the land the run's summary names, through the local forge the bare origin gets
    land = sandbox.cox("runs", "land", "ship-1", "--repo", "~/src/app", "--task", TASK, "--gate", "full", "--apply",
                       timeout=300)
    assert land.returncode == 0, land.stdout + land.stderr
    assert sandbox.git(repo.origin, "show", f"main:{NOTE}") == "ok"
    assert sandbox.store_row(INITIATIVE, TASK)["state"] == "done"


def test_a_run_whose_runner_cannot_be_built_ends_in_error_and_frees_its_lease(launchable, repo):
    sandbox = launchable.sandbox
    _filed(sandbox)
    # No answers file yet: the harness takes its lease ("graph started"), then the scripted runner cannot load. cox
    # reports the start when it sees that line first, or the failure when the process has already exited.
    assert not launchable.answers.exists()
    launch = sandbox.cox("route", "launch", "epic", "--initiative", f"work/{INITIATIVE}", "--repo", "~/src/app",
                         cwd=sandbox.workspace)
    assert launch.returncode in (0, 1), launch.stdout + launch.stderr
    runs = sandbox.workspace / "runs"
    if launch.returncode == 0:
        assert _wait_exit(int((runs / "ship-1.pid").read_text(encoding="utf-8")))
    else:
        assert "routing: launch failed: FileNotFoundError" in launch.stdout, launch.stdout
    log = (runs / "ship-1.log").read_text(encoding="utf-8")
    assert log.startswith("graph started: ship-1\n") and str(launchable.answers) in log, log[-2000:]
    ended = _run_row(sandbox, "ship-1")
    assert ended is not None and ended["status"] == "error" and ended["ended_at"], ended
    # the dead run released `runs:ship`: the next launch is not refused, and runs to completion
    _responses(launchable)
    assert SUMMARY.format(run="ship-2") in _launched(sandbox, "ship-2")


def test_a_single_graph_checks_its_patch_in_a_worktree_of_the_repo_and_records_the_run(launchable, repo):
    sandbox = launchable.sandbox
    result = sandbox.home / "life-1.result.json"
    argv = [str(sandbox.install.python), str(sandbox.install.root / "shell.py"), "lifecycle",
            *launchable.harness_flags(), "--run-id", "life-1", "--ticket", f"Add {NOTE} holding ok",
            "--repo", str(repo.path), "--scripted", str(_responses(launchable)), "--result-out", str(result)]
    done = launchable.run(argv, cwd=sandbox.workspace)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    assert f"patch applied in {launchable.worktrees / 'life-1'}" in done.stdout
    assert "recorded life-1: 0 auto-applied, 1 gated decision(s), 1 proposal(s)" in done.stdout

    record = json.loads(result.read_text(encoding="utf-8"))
    assert record["change_facts"]["patch_ok"] is True
    # the cartridge's check ran in the worktree, against the applied file
    assert [(c["name"], c["passed"]) for c in record["checks"]] == [("note", True), ("lane-path", True)], record.get("checks")
    assert [p["kind"] for p in record["proposals"]] == ["draft_pr_create"]
    assert not (launchable.worktrees / "life-1").exists()
    assert _registered_worktrees(sandbox, repo) == [str(repo.path)]
    run = _run_row(sandbox, "life-1")
    assert run is not None and run["status"] == "ok" and run["ended_at"], run
