"""`clean_phase` deletes the phase and draft branches on origin as well as locally, and never fails the land over it."""

import subprocess as sp

from agent_tools import cli

PHASE, DRAFT = "epic/i/p1", "epic/i/p1--t1"


def _git(repo, *args):
    return sp.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _step(tmp_path):
    return {"kind": "clean_phase", "run": "r1", "phase_branch": PHASE, "tasks": ["t1"], "worktree_root": str(tmp_path / "wt")}


def _repo(tmp_path, push=(PHASE, DRAFT), origin=True):
    """A checkout on main with both branches local, and a bare `origin` holding those named in `push`."""
    root, bare = tmp_path / "repo", tmp_path / "origin.git"
    sp.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    for args in (["config", "user.email", "t@e"], ["config", "user.name", "t"], ["commit", "-q", "--allow-empty", "-m", "init"],
                 ["branch", PHASE], ["branch", DRAFT]):
        _git(root, *args)
    if origin:
        sp.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        _git(root, "remote", "add", "origin", str(bare))
        for b in push:
            _git(root, "push", "-q", "origin", b)
    return root, bare


def _remote_heads(bare):
    return _git(bare, "for-each-ref", "--format=%(refname:short)", "refs/heads")


def test_both_branches_are_gone_from_origin_and_local(tmp_path):
    repo, bare = _repo(tmp_path)
    ok, detail = cli._execute_land_step(repo, _step(tmp_path))
    assert ok
    assert _remote_heads(bare) == ""
    assert _git(repo, "branch", "--list", "epic/*") == ""
    assert f"deleted on origin: {PHASE}, {DRAFT}" in detail


def test_a_branch_already_gone_from_origin_is_not_a_failure(tmp_path):
    repo, bare = _repo(tmp_path, push=(PHASE,))
    ok, detail = cli._execute_land_step(repo, _step(tmp_path))
    assert ok and "remote delete failed" not in detail
    assert _remote_heads(bare) == ""


def test_a_repo_with_no_origin_is_not_a_failure(tmp_path):
    repo, _ = _repo(tmp_path, origin=False)
    ok, detail = cli._execute_land_step(repo, _step(tmp_path))
    assert (ok, detail) == (True, f"deleted {PHASE}, {DRAFT}, agents/r1/t1")


def test_a_refused_remote_delete_is_reported_and_the_land_still_succeeds(tmp_path):
    repo, bare = _repo(tmp_path)
    hook = bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nwhile read old new ref; do\n  [ \"$new\" = 0000000000000000000000000000000000000000 ] && exit 1\ndone\nexit 0\n")
    hook.chmod(0o755)
    ok, detail = cli._execute_land_step(repo, _step(tmp_path))
    assert ok and "remote delete failed: epic/i/p1" in detail
    assert _remote_heads(bare) == f"{PHASE}\n{DRAFT}"
    assert _git(repo, "branch", "--list", "epic/*") == ""


def test_remote_doomed_keeps_only_the_initiative_namespace():
    names = [PHASE, DRAFT, "agents/r1/t1", "epic/other/p1", "pr/x", PHASE]
    assert cli._remote_doomed(names, "epic/i/") == [PHASE, DRAFT]
    assert cli._remote_doomed(names, "") == []
