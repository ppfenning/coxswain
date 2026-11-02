"""A phase is planned as already merged only when a PR merged and the phase branch adds nothing over main."""
import subprocess
from types import SimpleNamespace

import pytest

from agent_tools import cli, land

ITEMS = [{"id": "a", "status": "approved"}, {"id": "b", "status": "approved"}]
PHASE = {"run": "r", "phase": "p", "initiative": "i"}
RECORDS = [{"run": "r", "task": t, "phase": "p", "initiative": "i", "status": "approved",
            "review": {"verdict": "approve"}, "build": {"patch": "x"}} for t in ("a", "b")]
MERGED = [{"number": 535, "state": "merged"}]


def _kinds(already_merged):
    steps = land.land_plan(PHASE, {}, "main", items=ITEMS, task_records=RECORDS, already_merged=already_merged)
    return [s["kind"] for s in steps]


def test_a_merged_phase_plans_clean_phase_then_mark_done_per_task():
    assert _kinds(True) == ["clean_phase", "mark_done", "mark_done"]


def test_a_phase_not_known_merged_still_squashes():
    assert "squash_phase" in _kinds(False)


def git(repo, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _commit(repo, name, text):
    (repo / name).write_text(text + "\n")
    git(repo, "add", name)
    git(repo, "commit", "-qm", text)


def _repo(tmp_path, follow_up):
    """A clone whose main holds the squash of epic/i/p (b.txt); `follow_up` adds a commit to epic/i/p main lacks."""
    origin, clone = tmp_path / "origin.git", tmp_path / "clone"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(clone)], check=True)
    git(clone, "remote", "add", "origin", str(origin))
    _commit(clone, "a.txt", "base")
    git(clone, "checkout", "-qb", "epic/i/p")
    _commit(clone, "b.txt", "phase")
    if follow_up:
        _commit(clone, "c.txt", "follow-up")
    git(clone, "checkout", "-q", "main")
    _commit(clone, "b.txt", "phase")
    git(clone, "push", "-q", "origin", "main")
    return clone


def _landed(clone, prs, phase="p"):
    forge = SimpleNamespace(find_branch_prs=lambda repo, branch: prs)
    return cli._phase_already_landed(clone, "i", phase, "main", forge)


def test_a_merged_pr_and_a_phase_branch_that_adds_nothing_over_main_is_already_merged(tmp_path):
    assert _landed(_repo(tmp_path, False), MERGED) is True


def test_a_merged_pr_with_a_phase_branch_carrying_commits_main_lacks_is_not(tmp_path):
    assert _landed(_repo(tmp_path, True), MERGED) is False


def test_a_merged_pr_without_a_phase_branch_is_not(tmp_path):
    assert _landed(_repo(tmp_path, False), MERGED, phase="q") is False


@pytest.mark.parametrize("prs", [
    [*MERGED, {"number": 541, "state": "open"}], [{"number": 535, "state": "closed"}], [],
    "could not list pull requests for pr/i--p: HTTP 502"])
def test_no_merged_pr_or_an_open_one_is_not_already_merged(prs):
    assert cli._phase_already_merged(prs, True, True) is False
