"""The harness's entry point, run the way `cox route launch` starts it: `<harness_dir>/shell.py` -> `harness.cli.main`
-> `_run_graph`, under the install's interpreter, against real git and the test's Postgres.

Every node of a graph is a model call. These runs replay canned node answers through the harness's own offline runner
(`--scripted`, `runner.scripted.ScriptedRunner`), the one offline provider the harness has. Nothing else is stood in
for: cox builds the argv (its `--dry-run` prints it), the harness resolves the team cartridge and its skills, applies
the patch in a real worktree, runs the cartridge's check there, gates, merges onto the phase branch, pushes, and
records the run in the store. `cox route launch` itself cannot pass `--scripted`, so its detached start is covered
separately, up to the runner (`test_a_launch_starts_the_harness_detached_and_a_dead_run_frees_its_lease`).
"""

from __future__ import annotations

import json
import os
import time

import pytest

from smoke import plan

psycopg = pytest.importorskip("psycopg")

INITIATIVE, TASK, NOTE = "ship", "t1", plan.SMOKE_NOTE
EXIT_WAIT_S = 60


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


def test_an_epic_runs_offline_from_the_argv_cox_builds_and_its_phase_lands_on_main(launchable, repo):
    sandbox = launchable.sandbox
    _filed(sandbox)
    dry = sandbox.cox("route", "launch", "epic", "--initiative", f"work/{INITIATIVE}", "--repo", "~/src/app", "--dry-run",
                      cwd=sandbox.workspace)
    assert dry.returncode == 0, dry.stdout + dry.stderr
    argv = plan.dry_run_argv(dry.stdout)
    assert argv is not None, dry.stdout
    # cox starts the install's own interpreter on the harness_dir's launcher, with the run id it chose
    assert argv[:3] == [str(sandbox.install.python), str(sandbox.install.root / "shell.py"), "epic"]
    assert argv[argv.index("--run-id") + 1] == "ship-1"
    assert argv[argv.index("--base") + 1] == "origin/main"

    done = launchable.run([*argv, "--scripted", str(_responses(launchable))], cwd=sandbox.workspace)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    assert ("epic ship-1: 1 phase(s) complete, 0 partial, 0 blocked, 0 awaiting land, 0 task(s) quarantined"
            in done.stdout), done.stdout[-3000:]

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
    assert [(c["name"], c["passed"]) for c in record["checks"]] == [("note", True)], record.get("checks")
    assert [p["kind"] for p in record["proposals"]] == ["draft_pr_create"]
    assert not (launchable.worktrees / "life-1").exists()
    assert _registered_worktrees(sandbox, repo) == [str(repo.path)]
    run = _run_row(sandbox, "life-1")
    assert run is not None and run["status"] == "ok" and run["ended_at"], run


def _wait_exit(pid: int) -> bool:
    deadline = time.monotonic() + EXIT_WAIT_S
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.2)
    return False


def test_a_launch_starts_the_harness_detached_and_a_dead_run_frees_its_lease(launchable, repo):
    sandbox = launchable.sandbox
    _filed(sandbox)
    first = sandbox.cox("route", "launch", "epic", "--initiative", f"work/{INITIATIVE}", "--repo", "~/src/app",
                        cwd=sandbox.workspace)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "run ship-1" in first.stdout.splitlines()
    runs = sandbox.workspace / "runs"
    pid = int((runs / "ship-1.pid").read_text(encoding="utf-8"))
    assert json.loads((runs / "ship-1.launched.json").read_text(encoding="utf-8"))["initiative"] == INITIATIVE
    # The harness held its lease ("graph started"), then stopped at the runner: this provider profile names no model.
    assert _wait_exit(pid), f"the harness (pid {pid}) was still running after {EXIT_WAIT_S}s"
    log = (runs / "ship-1.log").read_text(encoding="utf-8")
    assert log.startswith("graph started: ship-1\n"), log[:2000]
    assert "provider profile must be a mapping with a 'tiers' block" in log, log[-2000:]
    ended = _run_row(sandbox, "ship-1")
    assert ended is not None and ended["status"] == "error" and ended["ended_at"], ended
    # the dead run released `runs:ship`, so the next launch is not refused
    second = sandbox.cox("route", "launch", "epic", "--initiative", f"work/{INITIATIVE}", "--repo", "~/src/app",
                         cwd=sandbox.workspace)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "run ship-2" in second.stdout.splitlines()
    assert _wait_exit(int((runs / "ship-2.pid").read_text(encoding="utf-8")))
