"""`python -m devtools release` end to end, against real git repositories with bare origins under tmp_path.

The release resumes a cut whose bump already landed on `release/<version>` while `main` moved on, merges that branch
back with a real merge commit, and leaves the tap PR it finds open. A second run of the same command must exit 0,
merge nothing twice and open no second pull request. GitHub is the one thing not real: `gh` on PATH is
`gh_stub.py`, which keeps pull requests and releases in a state file and merges with real git into the bare origin.
`https://github.com/` resolves to the bare origins through git's own `insteadOf`.
"""

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from devtools import release

_REPO = Path(__file__).resolve().parents[2]
_V = "2026.11.1"
_BRANCH = f"release/{_V}"
_REASON = "the end-to-end fixture has no rehearsal workflow"
_TIMEOUT = 120
_MANIFEST_AT = '[coxswain]\nversion = "{v}"\n'
_PYPROJECT_AT = '[project]\nname = "coxswain"\nversion = "{v}"\n'
_FORMULA_AT = ('class Cox < Formula\n  url "https://files.example/coxswain-{v}.tar.gz"\n  sha256 "' + "0" * 64 + '"\n'
               '  resource "dep" do\n    url "https://files.example/dep-1.0.tar.gz"\n  end\nend\n')


def _git(env, *args, cwd=None) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=_TIMEOUT)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _commit(env, work: Path, files: dict[str, str], message: str) -> None:
    for name, text in files.items():
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(text)
    _git(env, "-C", str(work), "add", "--", *files)
    _git(env, "-C", str(work), "commit", "-q", "-m", message)


def _environment(tmp_path: Path) -> dict[str, str]:
    home, bin_dir = tmp_path / "home", tmp_path / "bin"
    home.mkdir()
    bin_dir.mkdir()
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(f'[user]\n\tname = Release Test\n\temail = release@example.invalid\n'
                         f'[init]\n\tdefaultBranch = main\n'
                         f'[url "{tmp_path / "origins"}/"]\n\tinsteadOf = https://github.com/\n')
    gh = bin_dir / "gh"
    gh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{Path(__file__).with_name("gh_stub.py")}" "$@"\n')
    gh.chmod(0o755)
    keep = {k: v for k, v in os.environ.items() if k in ("LANG", "LC_ALL", "TMPDIR")}
    return {**keep, "HOME": str(home), "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
            "GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
            "GH_STUB_STATE": str(tmp_path / "gh-state.json"),
            "PYTHONPATH": os.pathsep.join([str(_REPO), str(_REPO / "src" / "tools")])}


def _notes() -> str:
    return (f"# {_V}\n\nThe release the end-to-end test cuts.\n\n"
            f"Cut with `--allow-no-rehearsal`: {_REASON}.\n")


def _umbrella_origin(env, tmp_path: Path) -> Path:
    """`main` at A then C, and `release/<version>` at B: A plus the bump, as an interrupted cut leaves it."""
    origin = tmp_path / "origins" / "ppfenning" / "coxswain.git"
    _git(env, "init", "-q", "--bare", str(origin))
    seed = tmp_path / "seed-coxswain"
    _git(env, "clone", "-q", str(origin), str(seed))
    bumped = tomllib.loads(_MANIFEST_AT.format(v=_V))
    _commit(env, seed, {"manifest.toml": _MANIFEST_AT.format(v="2026.11.0"),
                        "pyproject.toml": _PYPROJECT_AT.format(v="2026.11.0"),
                        f"docs/releases/{_V}.md": _notes(),
                        "docs/releases/index.md": release.release_index_text("", _V, bumped, tagged=set())},
            "the release notes")
    _git(env, "-C", str(seed), "push", "-q", "origin", "main")
    _git(env, "-C", str(seed), "checkout", "-q", "-b", _BRANCH)
    _commit(env, seed, {"manifest.toml": _MANIFEST_AT.format(v=_V), "pyproject.toml": _PYPROJECT_AT.format(v=_V)},
            f"release: bump to {_V} to match the tag")
    _git(env, "-C", str(seed), "push", "-q", "origin", _BRANCH)
    _git(env, "-C", str(seed), "checkout", "-q", "main")
    _commit(env, seed, {"README.md": "work that landed after the cut\n"}, "docs: a change after the cut")
    _git(env, "-C", str(seed), "push", "-q", "origin", "main")
    return origin


def _tap_origin(env, tmp_path: Path) -> Path:
    """`main` at the previous formula, and `release/<version>` already holding this version's, its PR open."""
    origin = tmp_path / "origins" / "ppfenning" / f"{release.TAP_CHECKOUT}.git"
    _git(env, "init", "-q", "--bare", str(origin))
    seed = tmp_path / "seed-tap"
    _git(env, "clone", "-q", str(origin), str(seed))
    _commit(env, seed, {"Formula/cox.rb": _FORMULA_AT.format(v="2026.11.0")}, "cox 2026.11.0")
    _git(env, "-C", str(seed), "push", "-q", "origin", "main")
    _git(env, "-C", str(seed), "checkout", "-q", "-b", _BRANCH)
    _commit(env, seed, {"Formula/cox.rb": _FORMULA_AT.format(v=_V)}, f"cox {_V}")
    _git(env, "-C", str(seed), "push", "-q", "origin", _BRANCH)
    Path(env["GH_STUB_STATE"]).write_text(json.dumps({
        "prs": [{"number": 1, "origin": str(origin), "head": _BRANCH, "base": "main", "title": f"cox {_V}",
                 "state": "open"}],
        "releases": {}, "proofs": [], "calls": []}))
    return origin


@pytest.fixture
def world(tmp_path):
    if subprocess.run(["git", "--version"], capture_output=True, timeout=_TIMEOUT).returncode != 0:
        pytest.skip("no git on PATH")
    env = _environment(tmp_path)
    umbrella_origin = _umbrella_origin(env, tmp_path)
    tap_origin = _tap_origin(env, tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    _git(env, "clone", "-q", str(umbrella_origin), str(work / "coxswain"))
    _git(env, "clone", "-q", str(tap_origin), str(work / release.TAP_CHECKOUT))
    return env, work, umbrella_origin


def _release(env, work: Path) -> subprocess.CompletedProcess:
    argv = [sys.executable, "-m", "devtools", "release", _V, "--manifest", str(work / "coxswain" / "manifest.toml"),
            "--allow-doc-drift", "the end-to-end fixture is not the real docs site",
            "--allow-no-rehearsal", _REASON]
    return subprocess.run(argv, cwd=str(work), env=env, capture_output=True, text=True, timeout=_TIMEOUT)


def _merges(env, origin: Path) -> list[str]:
    return _git(env, "--git-dir", str(origin), "rev-list", "--merges", "refs/heads/main").split()


def test_a_rerun_after_a_finished_merge_back_exits_0_and_merges_nothing_twice(world):
    env, work, origin = world
    first = _release(env, work)
    assert first.returncode == 0, first.stdout + first.stderr
    assert f"merge_back coxswain: {_BRANCH} merged into main" in first.stdout
    assert _git(env, "--git-dir", str(origin), "ls-remote", ".", f"refs/heads/{_BRANCH}") == ""
    merged = _merges(env, origin)
    assert len(merged) == 1
    tag = _git(env, "--git-dir", str(origin), "rev-parse", f"refs/tags/v{_V}^{{commit}}")
    assert _git(env, "--git-dir", str(origin), "merge-base", "--is-ancestor", tag, "refs/heads/main") == ""

    second = _release(env, work)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "tag_self coxswain: already merged back into the default branch" in second.stdout
    assert "merge_back coxswain: already merged back into the default branch" in second.stdout
    assert f"tap_formula_pr tap: {_BRANCH} already pushed with its pull request open" in second.stdout
    assert _merges(env, origin) == merged
    assert _git(env, "--git-dir", str(origin), "rev-parse", f"refs/tags/v{_V}^{{commit}}") == tag
    assert _git(env, "--git-dir", str(origin), "ls-remote", ".", f"refs/heads/{_BRANCH}") == ""
    state = json.loads(Path(env["GH_STUB_STATE"]).read_text())
    assert [pr["head"] for pr in state["prs"]] == [_BRANCH, _BRANCH]  # the tap's, then the one merge-back
    assert len([call for call in state["calls"] if call[:2] == ["release", "create"]]) == 1
    assert _git(env, "-C", str(work / "coxswain"), "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_a_rerun_after_a_merge_back_that_kept_its_branch_opens_no_pull_request(world):
    env, work, origin = world
    assert _release(env, work).returncode == 0
    merged = _merges(env, origin)
    # As if the merge had not deleted the branch: origin lists release/<version> again, at the head it merged.
    head = _git(env, "--git-dir", str(origin), "rev-parse", "refs/heads/main^2")
    _git(env, "--git-dir", str(origin), "update-ref", f"refs/heads/{_BRANCH}", head)

    again = _release(env, work)
    assert again.returncode == 0, again.stdout + again.stderr
    assert f"merge_back coxswain: {_BRANCH} already merged into main at {head}" in again.stdout
    assert _merges(env, origin) == merged
    state = json.loads(Path(env["GH_STUB_STATE"]).read_text())
    assert len(state["prs"]) == 2
    assert len([call for call in state["calls"] if call[:2] == ["pr", "create"]]) == 1
