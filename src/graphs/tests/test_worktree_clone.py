"""`worktree.clone_worktree` and `refresh_mirror` against real git: what our code decides about the mirror (create,
refresh, rebuild, discard, fall back) and the trace it reports. Git's own clone behaviour is not under test."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness import repo_cache, worktree


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _commit(work: Path, name: str) -> None:
    (work / name).write_text(f"{name}\n", encoding="utf-8")
    _git("add", name, cwd=work)
    _git("commit", "-qm", name, cwd=work)
    _git("push", "-q", "origin", "HEAD:main", cwd=work)


@pytest.fixture
def origin(tmp_path: Path) -> tuple[str, Path]:
    """A bare origin on `main` with three commits, reached over file:// (git's pack transport), and a seed to push from."""
    bare, seed = tmp_path / "origin.git", tmp_path / "seed"
    _git("init", "-q", "--bare", "-b", "main", str(bare))
    _git("init", "-q", "-b", "main", str(seed))
    _git("remote", "add", "origin", str(bare), cwd=seed)
    for name in ("a", "b", "c"):
        _commit(seed, name)
    return bare.as_uri(), seed


def _alternates(dest: Path) -> Path:
    return dest / ".git" / "objects" / "info" / "alternates"


def test_a_first_clone_creates_the_mirror_and_keeps_no_borrow(origin, tmp_path):
    url, _ = origin
    cache, dest = tmp_path / "cache", tmp_path / "lane1"
    cloned = worktree.clone_worktree(url, dest, cache_root=cache, branch="lane/one")
    assert cloned.ok, cloned.detail
    assert (cloned.mirror, cloned.borrowed, cloned.received) == ("created", True, 0)
    assert cloned.fetched > 0
    assert not _alternates(dest).exists()
    assert (dest / "c").read_text(encoding="utf-8") == "c\n"
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=dest) == "lane/one"
    assert repo_cache.mirror_dir(cache, url).is_dir()


def test_a_second_clone_fetches_only_what_the_remote_gained(origin, tmp_path):
    url, seed = origin
    cache = tmp_path / "cache"
    first = worktree.clone_worktree(url, tmp_path / "lane1", cache_root=cache)
    _commit(seed, "d")  # one commit, one tree, one blob
    second = worktree.clone_worktree(url, tmp_path / "lane2", cache_root=cache)
    assert second.ok, second.detail
    assert (second.mirror, second.fetched, second.received, second.borrowed) == ("refreshed", 3, 0, True)
    assert first.fetched > second.fetched
    assert _git("rev-parse", "HEAD", cwd=tmp_path / "lane2") == _git("rev-parse", "HEAD", cwd=seed)


def test_deleting_the_cache_changes_nothing_but_the_next_clones_cost(origin, tmp_path):
    url, _ = origin
    cache = tmp_path / "cache"
    warm = worktree.clone_worktree(url, tmp_path / "warm", cache_root=cache)
    subprocess.run(["rm", "-rf", str(cache)], check=True)
    _git("fsck", "--no-progress", cwd=tmp_path / "warm")  # the earlier clone never needed the mirror
    cold = worktree.clone_worktree(url, tmp_path / "cold", cache_root=cache)
    assert cold.ok and cold.mirror == "created" and cold.fetched == warm.fetched
    assert _git("rev-parse", "HEAD^{tree}", cwd=tmp_path / "cold") == _git("rev-parse", "HEAD^{tree}", cwd=tmp_path / "warm")


def test_a_path_that_is_not_a_bare_repository_is_rebuilt(origin, tmp_path):
    url, _ = origin
    cache = tmp_path / "cache"
    mirror = repo_cache.mirror_dir(cache, url)
    mirror.mkdir(parents=True)
    (mirror / "HEAD").write_text("garbage\n", encoding="utf-8")
    cloned = worktree.clone_worktree(url, tmp_path / "lane", cache_root=cache)
    assert cloned.ok and (cloned.mirror, cloned.borrowed) == ("rebuilt", True)


def test_a_scratch_mirror_a_crash_left_is_cleared_and_never_borrowed(origin, tmp_path):
    url, _ = origin
    cache = tmp_path / "cache"
    mirror = repo_cache.mirror_dir(cache, url)
    scratch = mirror.with_name(f"{mirror.name}.tmp-1-dead")
    scratch.mkdir(parents=True)
    state, fetched, _ = worktree.refresh_mirror(url, cache)
    assert (state, fetched > 0, scratch.exists()) == ("created", True, False)
    assert worktree.refresh_mirror(url, cache)[:2] == ("refreshed", 0)  # idempotent


def test_a_mirror_with_corrupt_objects_falls_back_to_a_plain_clone_and_is_dropped(origin, tmp_path):
    url, _ = origin
    cache = tmp_path / "cache"
    assert worktree.refresh_mirror(url, cache)[0] == "created"
    mirror = repo_cache.mirror_dir(cache, url)
    for found in (mirror / "objects").rglob("*"):
        if found.is_file() and found.parent.name not in ("info", "pack"):
            found.chmod(0o644)
            found.write_bytes(b"not an object")  # present by name, so no fetch replaces it; unreadable by content
    cloned = worktree.clone_worktree(url, tmp_path / "lane", cache_root=cache)
    assert cloned.ok, cloned.detail
    assert not cloned.borrowed and cloned.received > 0
    assert not _alternates(tmp_path / "lane").exists()
    assert (tmp_path / "lane" / "c").exists()
    assert not mirror.exists()  # dropped, so the next clone rebuilds it
    assert worktree.clone_worktree(url, tmp_path / "next", cache_root=cache).mirror == "created"


def test_an_unusable_cache_root_still_clones(origin, tmp_path):
    url, _ = origin
    blocked = tmp_path / "cache"
    blocked.write_text("a file, not a folder\n", encoding="utf-8")
    cloned = worktree.clone_worktree(url, tmp_path / "lane", cache_root=blocked)
    assert cloned.ok and (cloned.mirror, cloned.borrowed) == ("unavailable", False)
    assert cloned.received > 0


def test_a_remote_that_cannot_be_reached_keeps_the_mirror(origin, tmp_path):
    url, _ = origin
    cache = tmp_path / "cache"
    worktree.refresh_mirror(url, cache)
    gone = (tmp_path / "missing.git").as_uri()
    assert worktree.clone_worktree(gone, tmp_path / "lane", cache_root=cache).ok is False
    assert repo_cache.mirror_dir(cache, url).is_dir()
    assert not (tmp_path / "lane").exists()


def test_a_dest_that_already_holds_the_clone_is_left_alone_and_another_is_refused(origin, tmp_path):
    url, _ = origin
    cache, dest = tmp_path / "cache", tmp_path / "lane"
    assert worktree.clone_worktree(url, dest, cache_root=cache).ok
    again = worktree.clone_worktree(url, dest, cache_root=cache)
    assert (again.ok, again.mirror, again.fetched) == (True, "present", 0)
    other = tmp_path / "other"
    other.mkdir()
    (other / "keep").write_text("mine\n", encoding="utf-8")
    refused = worktree.clone_worktree(url, other, cache_root=cache)
    assert not refused.ok and (other / "keep").read_text(encoding="utf-8") == "mine\n"


def test_a_clone_a_crash_left_still_borrowing_is_cloned_again_not_reported_present(origin, tmp_path):
    url, _ = origin
    cache, dest = tmp_path / "cache", tmp_path / "lane"
    worktree.refresh_mirror(url, cache)
    # What a run killed between our clone and our repack leaves: the borrow still in place, nothing checked out.
    _git("clone", "-q", "--no-checkout", "--no-local", "--reference", str(repo_cache.mirror_dir(cache, url)), url, str(dest))
    assert _alternates(dest).exists()
    cloned = worktree.clone_worktree(url, dest, cache_root=cache)
    assert cloned.ok and cloned.mirror != "present" and cloned.borrowed, cloned
    assert not _alternates(dest).exists() and (dest / "c").exists()
    assert worktree.clone_worktree(url, dest, cache_root=cache).mirror == "present"  # now it is finished


def test_a_repository_of_another_remote_is_never_cleared(origin, tmp_path):
    url, _ = origin
    dest = tmp_path / "lane"
    _git("init", "-q", str(dest))
    _git("remote", "add", "origin", "https://elsewhere.invalid/x.git", cwd=dest)
    refused = worktree.clone_worktree(url, dest, cache_root=tmp_path / "cache")
    assert not refused.ok and (dest / ".git").is_dir()
