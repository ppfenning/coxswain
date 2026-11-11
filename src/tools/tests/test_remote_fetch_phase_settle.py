"""A lane fetch moves a local phase branch only forward; a diverged lane tip is kept on a side ref and reported."""

import json
import shutil
import subprocess

import pytest

from agent_tools.chair_exec import fetch_divergences
from agent_tools.lane_hosts import LaneHost
from agent_tools.remote_fetch import fetch_phase_branches, fetch_run, phase_update, task_repos

needs_tools = pytest.mark.skipif(
    shutil.which("rsync") is None or shutil.which("git") is None, reason="rsync and git are required"
)
PHASE = "epic/r1/p1"
SIDE = f"refs/coxswain/fetched/h/{PHASE}"


def _git(cwd, *args):
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    return subprocess.run(["git", *ident, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _real_run(argv):
    return subprocess.run(argv, capture_output=True).returncode


def _commit(repo, name):
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def _world(tmp_path):
    """A host repo with a task branch, and a chair repo that is a clone of it, so both share `main`'s first commit."""
    host_ws, chair_ws = tmp_path / "hostws", tmp_path / "chairws"
    host_repo, chair_repo = host_ws / "repo", chair_ws / "repo"
    host_repo.mkdir(parents=True)
    _git(host_repo, "init", "-q", "-b", "main")
    _commit(host_repo, "base.txt")
    _git(host_repo, "branch", "agents/r1/task")
    chair_ws.mkdir()
    _git(chair_ws, "clone", "-q", str(host_repo), "repo")
    task = host_ws / "runs" / "r1" / "tasks" / "p1" / "task.json"
    task.parent.mkdir(parents=True)
    task.write_text(json.dumps({"repo": str(host_repo)}))
    (host_ws / "runs" / "r1.log").write_text("log")
    return LaneHost("h", "u@h", str(host_ws)), chair_ws / "runs", host_repo, chair_repo


def _fetch(host, runs_dir):
    diverged: list[str] = []
    result = fetch_run(host, "r1", runs_dir, task_repos, _real_run, lambda p: p,
                       lease_released=True, ended_at="t", diverged=diverged)
    return result, diverged


def _tip(repo, ref):
    return _git(repo, "rev-parse", "--verify", ref)


def _no_ref(repo, ref):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref], capture_output=True).returncode != 0


@pytest.mark.parametrize(("local_exists", "local_in_fetched", "fetched_in_local", "verdict"), [
    (False, False, False, "create"),
    (True, True, False, "fast_forward"),
    (True, False, True, "keep"),
    (True, True, True, "keep"),
    (True, False, False, "diverged"),
])
def test_phase_update_only_ever_moves_a_local_phase_branch_forward(local_exists, local_in_fetched, fetched_in_local, verdict):
    assert phase_update(local_exists, local_in_fetched, fetched_in_local) == verdict


@needs_tools
def test_an_absent_local_phase_branch_is_created_from_the_fetched_tip(tmp_path):
    host, runs_dir, host_repo, chair_repo = _world(tmp_path)
    _git(host_repo, "checkout", "-q", "-b", PHASE)
    lane_tip = _commit(host_repo, "lane.txt")
    result, diverged = _fetch(host, runs_dir)
    assert result == (str(chair_repo),) and diverged == []
    assert _tip(chair_repo, f"refs/heads/{PHASE}") == lane_tip
    assert _no_ref(chair_repo, SIDE) and _no_ref(chair_repo, f"refs/coxswain/incoming/h/{PHASE}")


@needs_tools
def test_a_local_phase_branch_behind_the_fetched_tip_is_fast_forwarded(tmp_path):
    host, runs_dir, host_repo, chair_repo = _world(tmp_path)
    _git(chair_repo, "branch", PHASE, "main")
    _git(host_repo, "checkout", "-q", "-b", PHASE)
    lane_tip = _commit(host_repo, "lane.txt")
    _, diverged = _fetch(host, runs_dir)
    assert diverged == [] and _tip(chair_repo, f"refs/heads/{PHASE}") == lane_tip
    assert _no_ref(chair_repo, SIDE)


@needs_tools
def test_a_local_phase_branch_holding_a_merge_the_lane_lacks_is_kept_when_the_lane_reset_to_main(tmp_path):
    host, runs_dir, host_repo, chair_repo = _world(tmp_path)
    _git(chair_repo, "checkout", "-q", "-b", PHASE)
    merged = _commit(chair_repo, "merged.txt")
    _git(chair_repo, "checkout", "-q", "main")
    _git(host_repo, "branch", PHASE, "main")  # the lane started its phase branch fresh from main
    _, diverged = _fetch(host, runs_dir)
    assert diverged == [] and _tip(chair_repo, f"refs/heads/{PHASE}") == merged
    assert _no_ref(chair_repo, SIDE)


@needs_tools
def test_a_diverged_phase_branch_keeps_local_saves_the_lane_tip_and_reports_it(tmp_path):
    host, runs_dir, host_repo, chair_repo = _world(tmp_path)
    _git(chair_repo, "checkout", "-q", "-b", PHASE)
    merged = _commit(chair_repo, "merged.txt")
    _git(chair_repo, "checkout", "-q", "main")
    _git(host_repo, "checkout", "-q", "-b", PHASE)
    lane_tip = _commit(host_repo, "lane.txt")
    result, diverged = _fetch(host, runs_dir)
    assert result == (str(chair_repo),)
    assert _tip(chair_repo, f"refs/heads/{PHASE}") == merged
    assert _tip(chair_repo, SIDE) == lane_tip
    assert _no_ref(chair_repo, f"refs/coxswain/incoming/h/{PHASE}")
    assert len(diverged) == 1 and PHASE in diverged[0] and SIDE in diverged[0] and merged[:12] in diverged[0]
    assert fetch_divergences(f"r1: fetched from h\nr1: {diverged[0]}\n") == [f"r1: {diverged[0]}"]


@needs_tools
def test_fetching_one_phase_branch_by_name_settles_it_the_same_way(tmp_path):
    host, runs_dir, host_repo, chair_repo = _world(tmp_path)
    branch = "epic/other/p2"
    _git(chair_repo, "checkout", "-q", "-b", branch)
    merged = _commit(chair_repo, "merged.txt")
    _git(chair_repo, "checkout", "-q", "main")
    _git(host_repo, "branch", branch, "main")
    diverged: list[str] = []
    brought = fetch_phase_branches(host, "r1", runs_dir, [str(chair_repo)], [branch], _real_run, lambda p: p,
                                   diverged=diverged)
    assert brought == (branch,) and diverged == []
    assert _tip(chair_repo, f"refs/heads/{branch}") == merged
