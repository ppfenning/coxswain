"""`cox runs clean` deletes only the cleaned run's own branches, run through the installed `cox` against a real clone.

The run owns its `agents/<run>/...` namespace and the `epic/<initiative>/<phase>--<task>` merge targets its task
records name. Another initiative's merge target for a task of the same name is not the run's, with `--force` or
without (`cleanup.owned_branches`).
"""

import json


def _ok(done):
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def test_runs_clean_leaves_another_initiatives_merge_target_alone(sandbox, repo):
    sandbox.git(repo.path, "branch", "agents/r1/t1")
    sandbox.git(repo.path, "branch", "epic/dock/p1--t1")
    sandbox.write("runs/r1/tasks/p1/t1.json", json.dumps({"task": "t1", "landed": True}))
    _ok(sandbox.cox("runs", "clean", "r1", "--repo", "~/src/app", "--apply"))
    assert sandbox.git(repo.path, "branch", "--list", "epic/dock/*") == "epic/dock/p1--t1"


def test_runs_clean_force_deletes_the_runs_own_branches_and_no_other_initiatives(sandbox, repo):
    for branch in ("agents/r1/t1", "agents/r1/t2", "epic/r1/p1--t1", "epic/dock/p1--t1", "epic/dock/p1--t2"):
        sandbox.git(repo.path, "branch", branch)
    sandbox.write("runs/r1/tasks/p1/t1.json", json.dumps({"task": "t1", "reason": "attempts_exhausted"}))
    out = _ok(sandbox.cox("runs", "clean", "r1", "--repo", "~/src/app", "--apply", "--force"))
    assert "epic/dock" not in out, out
    assert sandbox.git(repo.path, "branch", "--list", "agents/r1/*", "epic/r1/*") == ""
    assert sandbox.git(repo.path, "branch", "--list", "epic/dock/*").split() == ["epic/dock/p1--t1", "epic/dock/p1--t2"]
