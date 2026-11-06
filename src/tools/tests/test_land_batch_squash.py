"""`squash_onto_batch`: each source branch becomes one attributed commit on a shared batch branch."""

import subprocess as sp
from pathlib import Path

import pytest

from agent_tools import cli
from agent_tools.land_batch_squash import squash_onto_batch


def _git(repo, *args):
    return sp.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(autouse=True)
def _own_tempdir(tmp_path, monkeypatch):
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "tmp"))


def _source(repo, name, path, text, author):
    _git(repo, "checkout", "-qb", name, "main")
    (repo / path).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"{name} work", "--author", f"{author} <{author.lower()}@e>")
    _git(repo, "checkout", "-q", "main")


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    sp.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    for args in (["config", "user.email", "t@e"], ["config", "user.name", "t"]):
        _git(root, *args)
    (root / "f.txt").write_text("original\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def _step(branch, source, first):
    return {"branch": branch, "onto": "batch/b1", "from": "main", "subject": f"epic {source}", "source": source, "first": first}


def test_two_disjoint_sources_give_two_attributed_commits(repo):
    _source(repo, "epic/a/p1", "a.txt", "a\n", "Alice")
    _source(repo, "epic/b/p2", "b.txt", "b\n", "Bob")
    ok1, d1 = squash_onto_batch(repo, _step("epic/a/p1", "a/p1", True))
    ok2, d2 = squash_onto_batch(repo, _step("epic/b/p2", "b/p2", False))
    assert (ok1, ok2) == (True, True), (d1, d2)
    assert _git(repo, "rev-list", "--count", "main..batch/b1") == "2"
    log = _git(repo, "log", "--reverse", "--format=%an|%ae|%s|%(trailers:key=Source,valueonly,separator=)", "batch/b1", "^main")
    assert [line for line in log.splitlines() if line] == ["Alice|alice@e|epic a/p1|a/p1", "Bob|bob@e|epic b/p2|b/p2"]
    assert _git(repo, "show", "batch/b1:a.txt") == "a" and _git(repo, "show", "batch/b1:b.txt") == "b"


def test_the_message_is_subject_blank_line_trailer(repo):
    _source(repo, "epic/a/p1", "a.txt", "a\n", "Alice")
    squash_onto_batch(repo, _step("epic/a/p1", "a/p1", True))
    assert _git(repo, "log", "-1", "--format=%B", "batch/b1").splitlines() == ["epic a/p1", "", "Source: a/p1"]


def test_a_conflicting_second_source_is_refused_and_leaves_one_commit(repo):
    _source(repo, "epic/a/p1", "f.txt", "from a\n", "Alice")
    _source(repo, "epic/b/p2", "f.txt", "from b\n", "Bob")
    assert squash_onto_batch(repo, _step("epic/a/p1", "a/p1", True))[0]
    head = _git(repo, "rev-parse", "batch/b1")
    ok, detail = squash_onto_batch(repo, _step("epic/b/p2", "b/p2", False))
    assert not ok
    assert detail.startswith("squash of epic/b/p2") and "f.txt" in detail
    assert _git(repo, "rev-list", "--count", "main..batch/b1") == "1"
    assert _git(repo, "rev-parse", "batch/b1") == head
    wt = cli._land_worktree(repo, "batch/b1")
    assert wt.is_dir() and _git(wt, "status", "--porcelain") == ""


def test_a_conflicting_first_source_removes_the_worktree_and_branch(repo):
    _git(repo, "checkout", "-qb", "epic/a/p1", "main")
    (repo / "f.txt").write_text("from a\n")
    _git(repo, "commit", "-qam", "a")
    _git(repo, "checkout", "-q", "main")
    # A base that has moved on the same line makes the squash conflict.
    _git(repo, "checkout", "-qb", "base2", "main")
    (repo / "f.txt").write_text("base moved\n")
    _git(repo, "commit", "-qam", "base moves")
    ok, detail = squash_onto_batch(repo, {**_step("epic/a/p1", "a/p1", True), "from": "base2"})
    assert not ok and detail.startswith("squash of epic/a/p1") and "f.txt" in detail
    assert _git(repo, "branch", "--list", "batch/b1") == ""
    assert not cli._land_worktree(repo, "batch/b1").exists()


def test_an_empty_source_commits_nothing(repo):
    _git(repo, "branch", "epic/e/p0", "main")
    ok, detail = squash_onto_batch(repo, _step("epic/e/p0", "e/p0", True))
    assert ok and "nothing to squash" in detail
    assert _git(repo, "rev-list", "--count", "main..batch/b1") == "0"


def test_a_later_call_without_a_first_call_is_refused(repo):
    _source(repo, "epic/a/p1", "a.txt", "a\n", "Alice")
    ok, detail = squash_onto_batch(repo, _step("epic/a/p1", "a/p1", False))
    assert not ok and detail.startswith("batch worktree missing")
