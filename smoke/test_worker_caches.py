"""E2, the worker's git mirror, end to end: `cox worker checkout` installed from this tree, against a real origin over
file:// (git's pack transport), in a home of its own. What is proved is our trace (`harness.worktree.Cloned`): what
the mirror fetched and what each clone took from the remote, not how git clones."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from smoke import plan


@dataclass(frozen=True)
class Machine:
    env: dict[str, str]
    cox: Path
    url: str
    seed: Path
    home: Path


def _git(env: dict[str, str], *args: str, cwd: Path | None = None) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, f"git {args[:2]}: {done.stderr.strip()}"
    return done.stdout.strip()


def _commit(m: Machine, name: str) -> None:
    (m.seed / name).write_text(f"{name}\n", encoding="utf-8")
    _git(m.env, "add", name, cwd=m.seed)
    _git(m.env, "commit", "-qm", name, cwd=m.seed)
    _git(m.env, "push", "-q", "origin", "HEAD:main", cwd=m.seed)


@pytest.fixture
def machine(tmp_path: Path, install) -> Machine:
    """One worker machine: its own home (so the cache is `~/.cache/coxswain`, inside the sandbox) and an origin."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(plan.gitconfig_text(), encoding="utf-8")
    env = {**install.env, "HOME": str(home), "XDG_CACHE_HOME": str(home / ".cache"),
           "GIT_CONFIG_GLOBAL": str(home / ".gitconfig")}
    env.pop("COX_CACHE_DIR", None)
    bare, seed = tmp_path / "origin.git", tmp_path / "seed"
    _git(env, "init", "-q", "--bare", "-b", "main", str(bare))
    _git(env, "init", "-q", "-b", "main", str(seed))
    _git(env, "remote", "add", "origin", str(bare), cwd=seed)
    m = Machine(env=env, cox=install.cox, url=bare.as_uri(), seed=seed, home=home)
    for name in ("one", "two", "three", "four"):
        _commit(m, name)
    return m


def _start(m: Machine, dest: Path) -> subprocess.Popen[str]:
    return subprocess.Popen([str(m.cox), "worker", "checkout", m.url, str(dest), "--branch", f"lane/{dest.name}",
                             "--json"], env=m.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _finish(proc: subprocess.Popen[str]) -> dict:
    out, err = proc.communicate(timeout=300)
    assert proc.returncode == 0, f"cox worker checkout exited {proc.returncode}: {err.strip() or out.strip()}"
    return json.loads(out)


def _checkout(m: Machine, dest: Path) -> dict:
    return _finish(_start(m, dest))


def test_a_second_lane_fetches_only_the_objects_origin_gained(machine: Machine, tmp_path: Path) -> None:
    first = _checkout(machine, tmp_path / "lane1")
    assert Path(first["cache_root"]) == machine.home / ".cache" / "coxswain"
    assert (first["mirror"], first["borrowed"], first["received"]) == ("created", True, 0)
    _commit(machine, "five")  # one commit, one tree, one blob
    second = _checkout(machine, tmp_path / "lane2")
    assert (second["mirror"], second["fetched"], second["received"], second["borrowed"]) == ("refreshed", 3, 0, True)
    assert first["fetched"] > second["fetched"]
    assert _git(machine.env, "rev-parse", "HEAD", cwd=tmp_path / "lane2") == _git(machine.env, "rev-parse", "HEAD",
                                                                                   cwd=machine.seed)
    assert _git(machine.env, "rev-parse", "--abbrev-ref", "HEAD", cwd=tmp_path / "lane2") == "lane/lane2"


def test_deleting_the_cache_directory_changes_nothing_but_time(machine: Machine, tmp_path: Path) -> None:
    warm = _checkout(machine, tmp_path / "warm")
    shutil.rmtree(warm["cache_root"])
    # The lane cloned before the deletion holds every object itself: nothing it needs was in the cache.
    _git(machine.env, "fsck", "--no-progress", cwd=tmp_path / "warm")
    assert not (tmp_path / "warm" / ".git" / "objects" / "info" / "alternates").exists()
    cold = _checkout(machine, tmp_path / "cold")
    assert (cold["mirror"], cold["fetched"], cold["borrowed"]) == ("created", warm["fetched"], True)
    trees = {_git(machine.env, "rev-parse", "HEAD^{tree}", cwd=tmp_path / name) for name in ("warm", "cold")}
    assert len(trees) == 1


def test_two_lanes_created_at_once_both_succeed(machine: Machine, tmp_path: Path) -> None:
    procs = [_start(machine, tmp_path / name) for name in ("left", "right")]
    results = [_finish(proc) for proc in procs]
    # The exclusive lock lets one make the mirror; the other then finds it and only refreshes it.
    assert sorted(r["mirror"] for r in results) == ["created", "refreshed"]
    assert all(r["borrowed"] and r["received"] == 0 for r in results)
    assert sorted(r["fetched"] == 0 for r in results) == [False, True]
    heads = {_git(machine.env, "rev-parse", "HEAD", cwd=tmp_path / name) for name in ("left", "right")}
    assert heads == {_git(machine.env, "rev-parse", "HEAD", cwd=machine.seed)}
