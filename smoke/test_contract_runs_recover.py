"""Contract: `cox runs recover` <-> git and the store, on a real repository.

Recover merges an approved task's one commit into its phase branch in a temporary worktree, then closes the work item
in the file and, through `harness.store_cli set-state`, in the store. Its unit tests check the plan (`land.recover_plan`)
on a dict of branches; this runs the git argv, the worktree add and remove, and the store write for real. The repo is
named in the tilde form a profile records (`~/src/app`): a tilde handed to git unexpanded is the bug #572 fixed for a
partial land.
"""

import json

from smoke import plan

TASK = {"state": "approved", "title": "First task", "id": "t1", "phase": "p1", "repo": "app"}
PHASE, SOURCE = "epic/ship/p1", "agents/ship-1/t1"


def _task_branch(sandbox, repo):
    sandbox.git(repo.path, "branch", PHASE)
    sandbox.git(repo.path, "checkout", "--quiet", "-b", SOURCE, PHASE)
    (repo.path / "feature.txt").write_text("feature\n", encoding="utf-8")
    sandbox.git(repo.path, "add", "feature.txt")
    sandbox.git(repo.path, "commit", "--quiet", "-m", "t1: add the feature")
    sandbox.git(repo.path, "checkout", "--quiet", "main")


def _approved_task(sandbox):
    sandbox.write("runs/ship-1/tasks/p1/t1.json", json.dumps({"ticket": "t1"}))
    path = sandbox.write("work/ship/p1/t1.md", plan.work_item_text(TASK, "Task body"))
    imported = sandbox.cox("route", "import")
    assert imported.returncode == 0, imported.stdout + imported.stderr
    return path


def test_runs_recover_merges_the_task_into_its_phase_branch_and_closes_it(sandbox, repo):
    _task_branch(sandbox, repo)
    path = _approved_task(sandbox)
    main_before = sandbox.git(repo.path, "rev-parse", "main")

    done = sandbox.cox("runs", "recover", "ship-1", "t1", "--repo", "~/src/app")

    assert done.returncode == 0, done.stdout + done.stderr
    assert sandbox.git(repo.path, "log", "-1", "--format=%s", PHASE) == f"Merge branch '{SOURCE}' into {PHASE}"
    assert sandbox.git(repo.path, "show", f"{PHASE}:feature.txt") == "feature"
    assert sandbox.git(repo.path, "rev-parse", "main") == main_before
    assert len(sandbox.git(repo.path, "worktree", "list").splitlines()) == 1
    assert plan.front_matter(path.read_text(encoding="utf-8"))["state"] == "done"
    assert sandbox.store_row("ship", "t1")["state"] == "done"


def test_runs_recover_a_second_time_finds_the_commit_already_there(sandbox, repo):
    _task_branch(sandbox, repo)
    _approved_task(sandbox)
    first = sandbox.cox("runs", "recover", "ship-1", "t1", "--repo", "~/src/app")
    assert first.returncode == 0, first.stdout + first.stderr
    merged = sandbox.git(repo.path, "rev-parse", PHASE)

    again = sandbox.cox("runs", "recover", "ship-1", "t1", "--repo", "~/src/app")

    assert again.returncode == 0, again.stdout + again.stderr
    assert "already contains the commit" in again.stdout
    assert sandbox.git(repo.path, "rev-parse", PHASE) == merged


def test_runs_recover_refuses_without_a_phase_branch_and_changes_nothing(sandbox, repo):
    path = _approved_task(sandbox)

    done = sandbox.cox("runs", "recover", "ship-1", "t1", "--repo", "~/src/app")

    assert done.returncode == 2, done.stdout + done.stderr
    assert f"phase branch {PHASE} does not exist" in done.stdout
    assert plan.front_matter(path.read_text(encoding="utf-8"))["state"] == "approved"
    assert sandbox.store_row("ship", "t1")["state"] == "approved"
