"""A `cherry_pick` land step picks every commit its branch is ahead of `from`, oldest first."""

import subprocess as sp

import pytest

from agent_tools import cli


def _git(repo, *args):
    return sp.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(autouse=True)
def _own_tempdir(tmp_path, monkeypatch):
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "tmp"))


def _repo_ahead(tmp_path, count):
    """`main` with one commit and an `epic/x/p1` branch `count` commits ahead of it, each adding its own file."""
    root = tmp_path / "repo"
    sp.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    for args in (["config", "user.email", "t@e"], ["config", "user.name", "t"]):
        _git(root, *args)
    (root / "base").write_text("x")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    _git(root, "checkout", "-qb", "epic/x/p1")
    for n in range(1, count + 1):
        (root / f"f{n}").write_text(str(n))
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", f"c{n}")
    _git(root, "checkout", "-q", "main")
    return root


STEP = {"kind": "cherry_pick", "branch": "epic/x/p1", "onto": "pr/x", "from": "main"}


def test_three_commits_are_picked_in_order_onto_the_pr_branch(tmp_path):
    repo = _repo_ahead(tmp_path, 3)
    ok, detail = cli._execute_land_step(repo, {**STEP, "commits": 3})
    assert (ok, detail) == (True, "cherry-picked 3 commits onto pr/x")
    assert _git(repo, "log", "--reverse", "--format=%s", "main..pr/x").split() == ["c1", "c2", "c3"]


def test_a_commits_mismatch_refuses_naming_both_counts_and_the_branch(tmp_path):
    repo = _repo_ahead(tmp_path, 2)
    ok, detail = cli._execute_land_step(repo, {**STEP, "commits": 3})
    assert (ok, detail) == (False, "expected 3 commits ahead of main on epic/x/p1, found 2")
    assert _git(repo, "rev-list", "--count", "main..pr/x") == "0"


def test_a_conflict_on_the_second_commit_names_its_sha_and_branch_and_leaves_no_pick_running(tmp_path):
    repo = _repo_ahead(tmp_path, 2)
    _git(repo, "checkout", "-q", "epic/x/p1")
    (repo / "base").write_text("b")
    _git(repo, "commit", "-qam", "c3")
    _git(repo, "checkout", "-q", "main")
    (repo / "base").write_text("m")
    _git(repo, "commit", "-qam", "main moves")
    sha = _git(repo, "rev-parse", "--short=8", "epic/x/p1")
    ok, detail = cli._execute_land_step(repo, {**STEP, "commits": 3})
    wt = cli._land_worktree(repo, "pr/x")
    assert not ok
    assert detail.startswith(f"cherry-pick of {sha} conflicts in: base\n")
    assert detail.endswith(f"\nstopped at {sha} on epic/x/p1")
    assert _git(wt, "log", "--reverse", "--format=%s", "main..pr/x").split() == ["c1", "c2"]
    assert sp.run(["git", "-C", str(wt), "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD"]).returncode != 0


def test_a_one_commit_step_with_no_commits_key_still_lands(tmp_path):
    repo = _repo_ahead(tmp_path, 1)
    ok, detail = cli._execute_land_step(repo, STEP)
    assert (ok, detail) == (True, "cherry-picked 1 commits onto pr/x")
    assert _git(repo, "log", "--format=%s", "main..pr/x") == "c1"
