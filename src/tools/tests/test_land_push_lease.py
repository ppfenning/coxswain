import subprocess
from types import SimpleNamespace

from agent_tools import cli, forge_github, land

BRANCH = "pr/remote-launch--p1"


def test_push_lease_replaces_the_tip_when_every_pr_is_closed():
    prs = [{"number": 518, "state": "closed"}, {"number": 521, "state": "closed"}]
    assert land.push_lease("abc123", prs) == {"kind": "lease", "tip": "abc123", "prs": [518, 521]}


def test_push_lease_is_plain_for_an_open_merged_or_unknown_pr_beside_closed_ones():
    closed = {"number": 1, "state": "closed"}
    for state in ("open", "merged", "unknown"):
        assert land.push_lease("abc123", [closed, {"number": 2, "state": state}]) == {"kind": "plain"}


def test_push_lease_is_plain_with_no_remote_branch_or_no_pr():
    assert land.push_lease(None, [{"number": 1, "state": "closed"}]) == {"kind": "plain"}
    assert land.push_lease("abc123", []) == {"kind": "plain"}


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True, text=True)


def _commit(repo, name):
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", name)


def _repo_with_leftover(tmp_path):
    """A clone whose `BRANCH` holds a commit unrelated to the one origin already has under that name."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    old, repo = tmp_path / "old", tmp_path / "repo"
    for path in (old, repo):
        subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
        _git(path, "remote", "add", "origin", str(origin))
        _commit(path, "base" if path is repo else "closed-pr-work")
    _git(old, "push", "-q", "origin", f"main:refs/heads/{BRANCH}")
    _git(repo, "branch", BRANCH)
    return repo, origin


def _forge(prs):
    return SimpleNamespace(find_branch_prs=lambda repo, branch: prs, push=forge_github.push)


def _tip(origin):
    return subprocess.run(["git", "-C", str(origin), "rev-parse", BRANCH], capture_output=True, text=True).stdout.strip()


def test_a_land_onto_a_branch_left_by_a_closed_pr_replaces_it(tmp_path):
    repo, origin = _repo_with_leftover(tmp_path)
    before = _tip(origin)
    ok, detail = cli._land_push(repo, BRANCH, _forge([{"number": 521, "state": "closed"}]))
    assert ok, detail
    assert f"replaced {before[:8]} left by closed #521" in detail
    assert _tip(origin) == subprocess.run(["git", "-C", str(repo), "rev-parse", BRANCH], capture_output=True, text=True).stdout.strip()


def test_a_land_onto_a_branch_with_an_open_pr_is_still_refused(tmp_path):
    repo, origin = _repo_with_leftover(tmp_path)
    before = _tip(origin)
    ok, detail = cli._land_push(repo, BRANCH, _forge([{"number": 521, "state": "open"}]))
    assert not ok
    assert "rejected" in detail
    assert _tip(origin) == before


def test_a_land_onto_a_branch_with_a_merged_pr_is_still_refused(tmp_path):
    repo, origin = _repo_with_leftover(tmp_path)
    before = _tip(origin)
    ok, _ = cli._land_push(repo, BRANCH, _forge([{"number": 5, "state": "closed"}, {"number": 6, "state": "merged"}]))
    assert not ok
    assert _tip(origin) == before


def test_a_forge_that_cannot_list_prs_gets_the_plain_push(tmp_path):
    repo, origin = _repo_with_leftover(tmp_path)
    before = _tip(origin)
    ok, _ = cli._land_push(repo, BRANCH, SimpleNamespace(find_branch_prs=lambda r, b: "gh failed", push=forge_github.push))
    assert not ok
    assert _tip(origin) == before
