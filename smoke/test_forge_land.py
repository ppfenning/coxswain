"""The forge path, end to end: `cox runs land --apply` pushes, opens a pull request, waits for its checks and merges
it, on a real Forgejo, through the forge that `forge: auto` picks for the repository's origin (`forge_forgejo`).

The test plays the CI: while the land runs, it waits for the pushed branch to appear on the forge and reports a commit
status on its tip, as a CI job would. Nothing in `cox` is stubbed; the server answers every call.
"""

import json
import subprocess
import time

import pytest

from smoke import plan

pytestmark = pytest.mark.forge

TASK = {"state": "approved", "title": "First task", "id": "t1", "phase": "p1", "repo": "app"}
RUN, SOURCE, PR_BRANCH = "land-1", "agents/land-1/t1", "pr/t1"
# What a build run records for an approved task: its draft PR proposal and the reviewers' verdicts.
RECORD = {"ticket": "t1", "initiative": "ship", "proposals": [{"kind": "draft_pr_create", "title": "t1: add the feature"}],
          "review": {"verdict": "approve"}, "adversary": {"verdict": "approve"}}
LAND_TIMEOUT_S = 180


def _approved_task(sandbox, repo):
    sandbox.git(repo.path, "checkout", "--quiet", "-b", SOURCE)
    (repo.path / "feature.txt").write_text("feature\n", encoding="utf-8")
    sandbox.git(repo.path, "add", "feature.txt")
    sandbox.git(repo.path, "commit", "--quiet", "-m", "t1: add the feature")
    sandbox.git(repo.path, "checkout", "--quiet", "main")
    sandbox.write(f"runs/{RUN}/tasks/p1/t1.json", json.dumps(RECORD))
    sandbox.write("work/ship/p1/t1.md", plan.work_item_text(TASK, "Task body"))
    imported = sandbox.cox("route", "import")
    assert imported.returncode == 0, imported.stdout + imported.stderr


def _land_reporting(sandbox, repo, state):
    """Run the land, and report `state` on the tip of the branch it pushes as soon as the forge has it."""
    argv = [str(sandbox.install.cox), "runs", "land", RUN, "--task", "t1", "--repo", "~/src/app", "--gate", "full", "--apply"]
    with subprocess.Popen(argv, env=sandbox.env, cwd=sandbox.home, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True) as land:
        deadline = time.monotonic() + LAND_TIMEOUT_S
        tip = None
        while tip is None and land.poll() is None and time.monotonic() < deadline:
            tip = repo.branch_tip(PR_BRANCH)
            time.sleep(0.2)
        if tip is not None:
            repo.report(tip, state)
        try:
            output, _ = land.communicate(timeout=max(deadline - time.monotonic(), 1))
        except subprocess.TimeoutExpired:
            land.kill()
            output, _ = land.communicate(timeout=30)
            pytest.fail(f"the land did not finish in {LAND_TIMEOUT_S}s:\n{output}")
    return land.returncode, output, tip


def test_a_land_opens_a_pull_request_waits_for_green_checks_and_merges_it(forge_sandbox, forge_repo):
    _approved_task(forge_sandbox, forge_repo)

    code, output, tip = _land_reporting(forge_sandbox, forge_repo, "success")

    assert code == 0, output
    assert "forge: auto" in output
    pull = forge_repo.pull(1)  # the repository is new, so the land's pull request is the first
    assert pull["merged"] and pull["base"] == "main", pull
    assert pull["sha"] == tip
    assert forge_repo.file_text("main", "feature.txt") == "feature\n"
    assert forge_repo.branch_tip(PR_BRANCH) is None  # the merge deleted the pull request's branch
    # the local default branch follows the forge's after the merge
    forge_sandbox.git(forge_repo.path, "fetch", "--quiet", "origin")
    assert forge_sandbox.git(forge_repo.path, "rev-parse", "main") == forge_sandbox.git(forge_repo.path, "rev-parse", "origin/main")


def test_a_land_whose_checks_fail_leaves_the_pull_request_open_and_main_alone(forge_sandbox, forge_repo):
    _approved_task(forge_sandbox, forge_repo)
    main_before = forge_repo.branch_tip("main")

    code, output, tip = _land_reporting(forge_sandbox, forge_repo, "failure")

    assert code != 0, output
    assert "failing checks: ci" in output
    pull = forge_repo.pull(1)
    assert pull["state"] == "open" and not pull["merged"] and pull["head"] == PR_BRANCH, pull
    assert pull["sha"] == tip
    assert forge_repo.branch_tip("main") == main_before
    assert forge_repo.file_text("main", "feature.txt") is None
