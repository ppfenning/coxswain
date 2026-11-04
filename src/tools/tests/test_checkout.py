import subprocess
from pathlib import Path

import pytest

from agent_tools import checkout
from agent_tools.checkout import _choose_listing, _default_branch, read_checkout


@pytest.fixture(autouse=True)
def _fresh_fetch_memo():
    checkout._fetch_origin.cache_clear()
    yield
    checkout._fetch_origin.cache_clear()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _write(root: Path, rel: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x")


def test_origin_main_listing_ignores_untracked_file(tmp_path):
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "src/a.py")
    _write(tmp_path, "README.md")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    _write(tmp_path, "untracked.txt")
    assert read_checkout(tmp_path) == frozenset({"src/a.py", "README.md"})


def test_plain_directory_falls_back_to_working_tree_without_git_dir(tmp_path):
    _write(tmp_path, "a/b.txt")
    _write(tmp_path, "top.txt")
    _write(tmp_path, ".git/config")
    assert read_checkout(tmp_path) == frozenset({"a/b.txt", "top.txt"})


def test_plain_directory_inside_another_repository_is_walked_not_read_as_that_repository(tmp_path):
    outer = tmp_path / "outer"
    _write(outer, "tracked.py")
    _git(outer, "init", "-q")
    _git(outer, "add", ".")
    _git(outer, "commit", "-q", "-m", "init")
    _git(outer, "update-ref", "refs/remotes/origin/main", "HEAD")
    _write(outer, "inner/only.txt")
    assert read_checkout(outer / "inner") == frozenset({"only.txt"})


def test_missing_directory_is_empty(tmp_path):
    assert read_checkout(tmp_path / "missing") == frozenset()


def test_choose_listing_by_literals():
    assert _choose_listing("a\0b/c\0", frozenset({"x"})) == frozenset({"a", "b/c"})
    assert _choose_listing(None, frozenset({"x"})) == frozenset({"x"})
    assert _choose_listing("", frozenset({"x"})) == frozenset(), "a readable ref with an empty tree is still the ref"


def test_default_branch_by_literals():
    assert _default_branch("refs/remotes/origin/develop\n") == "develop"
    assert _default_branch("refs/remotes/origin/release/2026") == "release/2026"
    assert _default_branch(None) == "main"
    assert _default_branch("") == "main"
    assert _default_branch("refs/heads/trunk") == "main", "an unrecognised ref is not guessed at"
    assert _default_branch("refs/remotes/origin/") == "main"


def _remote_and_clone(tmp_path: Path, branch: str = "main") -> tuple[Path, Path]:
    """A bare remote holding `seed.txt` on `branch`, and a clone of it whose origin/HEAD is `branch`."""
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", "--bare", "-b", branch, str(remote))
    _git(tmp_path, "init", "-q", "-b", branch, str(seed))
    _write(seed, "seed.txt")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "seed")
    _git(seed, "push", "-q", str(remote), branch)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(remote), str(clone))
    return remote, clone


def _push_file(tmp_path: Path, remote: Path, rel: str, branch: str = "main") -> None:
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(remote), str(other))
    _write(other, rel)
    _git(other, "add", ".")
    _git(other, "commit", "-q", "-m", "add")
    _git(other, "push", "-q", "origin", branch)


def test_file_on_remote_default_branch_missing_from_stale_clone_is_listed(tmp_path):
    remote, clone = _remote_and_clone(tmp_path)
    _push_file(tmp_path, remote, "new/feature.py")
    assert not (clone / "new/feature.py").exists()
    listing = read_checkout(clone)
    assert "new/feature.py" in listing
    assert "seed.txt" in listing
    assert "nowhere.py" not in listing


def test_non_main_default_branch_is_read_from_that_branch(tmp_path):
    remote, clone = _remote_and_clone(tmp_path, branch="develop")
    _push_file(tmp_path, remote, "dev_only.py", branch="develop")
    assert read_checkout(clone) == frozenset({"seed.txt", "dev_only.py"})


def test_failed_fetch_still_returns_the_local_origin_listing(tmp_path):
    _, clone = _remote_and_clone(tmp_path)
    _git(clone, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    assert read_checkout(clone) == frozenset({"seed.txt"})


def test_two_reads_of_one_repo_fetch_once(tmp_path, monkeypatch):
    _, clone = _remote_and_clone(tmp_path)
    real_run = subprocess.run
    fetches = []

    def counting_run(argv, *args, **kwargs):
        if "fetch" in argv:
            fetches.append(argv)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(checkout.subprocess, "run", counting_run)
    read_checkout(clone)
    read_checkout(clone)
    assert len(fetches) == 1
