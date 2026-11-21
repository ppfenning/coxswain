"""The smoke suite's shell, where it needs no sandbox: no Postgres, no install, no `cox`.

`gh_replay.real_gh` resolving a mise shim (moved from smoke/test_contract_gh.py, which keeps the tests that run the
real `gh`), and the conftest's refusal to load packages from anywhere but this tree (the guard that replaced
smoke/test_imports_this_tree.py). Real files and real processes; nothing is replaced.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from smoke import gh_replay, plan

ROOT = Path(__file__).resolve().parents[1]


def _executable(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_a_mise_shim_for_gh_resolves_to_the_binary_mise_names(tmp_path):
    # A real shim layout: bin/gh links to an executable named `mise`, which answers `which gh` as mise does.
    real = _executable(tmp_path / "real-gh", "#!/bin/sh\nexit 0\n")
    mise = _executable(tmp_path / "mise", f'#!/bin/sh\n[ "$1 $2" = "which gh" ] && echo {real} && exit 0\nexit 2\n')
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "gh").symlink_to(mise)
    assert gh_replay.real_gh(str(tmp_path / "bin" / "gh")) == str(real)


def test_a_shim_mise_cannot_resolve_fails_with_mises_stderr(tmp_path):
    mise = _executable(tmp_path / "mise", "#!/bin/sh\necho 'gh is not installed' >&2\nexit 1\n")
    (tmp_path / "gh").symlink_to(mise)
    with pytest.raises(RuntimeError, match="gh is not installed"):
        gh_replay.real_gh(str(tmp_path / "gh"))


def test_a_plain_gh_binary_is_its_own_answer(tmp_path):
    gh = _executable(tmp_path / "gh", "#!/bin/sh\nexit 0\n")
    assert gh_replay.real_gh(str(gh)) == str(gh)


def test_the_committed_timings_name_every_smoke_file_and_no_deleted_one():
    """The shards fall back gracefully on a file with no recorded seconds (`plan.file_weights`), so nothing else would
    notice a stale timings file: this does. A forge-only file is named too, at 0 (`plan.file_seconds`)."""
    timings = plan.read_timings((ROOT / "smoke" / plan.TIMINGS_FILE).read_text(encoding="utf-8"))
    files = {f"smoke/{path.name}" for path in (ROOT / "smoke").glob("test_*.py")}
    regenerate = f'regenerate it: {plan.TIMINGS_ENV}=smoke/{plan.TIMINGS_FILE} pytest -p smoke.timings -m "not forge" smoke'
    assert sorted(files - timings.keys()) == [], f"smoke files with no recorded seconds; {regenerate}"
    assert sorted(timings.keys() - files) == [], f"{plan.TIMINGS_FILE} names deleted files; drop them or {regenerate}"


def _collect_smoke(pythonpath: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": pythonpath}
    return subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "smoke"],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)


def test_a_stale_checkout_ahead_on_the_path_does_not_shadow_this_tree(tmp_path: Path) -> None:
    """A chair lane's failure, rebuilt: an older checkout's packages sit ahead of this tree on the path, and its `core`
    has a `jsontypes` without `conform`, as the stale checkout's did. The smoke conftest still loads, and its guard
    finds agent_tools, harness and core in this tree."""
    stale = tmp_path / "stale-checkout"
    for name in ("agent_tools", "harness", "core"):
        (stale / name).mkdir(parents=True)
        (stale / name / "__init__.py").write_text(f'"""A stale checkout\'s {name}."""\n')
    (stale / "core" / "jsontypes.py").write_text('"""A stale checkout\'s jsontypes: no `conform`."""\n')
    inherited = os.environ.get("PYTHONPATH", "")
    done = _collect_smoke(os.pathsep.join(p for p in (str(stale), inherited) if p))
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]


def _ignore(folder: str, names: list[str]) -> set[str]:
    return {name for name in names if name in {"tests", "__pycache__"}}


def test_the_conftest_refuses_a_tree_whose_path_fix_no_longer_covers_harness_and_core(tmp_path: Path) -> None:
    """The guard pinned: a copy of this tree whose conftest puts only src/tools ahead (the fix as it first was), with a
    stale checkout ahead of it on the path that holds real, importable copies of `harness` and `core`. Every import
    succeeds, so only the guard can refuse it, and it must, naming the package and this tree's src/ folder."""
    tree, stale = tmp_path / "tree", tmp_path / "stale-checkout"
    shutil.copytree(ROOT / "src" / "tools" / "agent_tools", tree / "src" / "tools" / "agent_tools", ignore=_ignore)
    shutil.copytree(ROOT / "smoke", tree / "smoke", ignore=lambda folder, names: {n for n in names if n.startswith("test_")})
    shutil.copytree(ROOT / "src" / "graphs" / "harness", stale / "harness", ignore=_ignore)
    shutil.copytree(ROOT / "src" / "cartridges" / "core", stale / "core", ignore=_ignore)
    conftest = tree / "smoke" / "conftest.py"
    fix = 'for name in ("tools", "graphs", "cartridges")]'
    assert fix in conftest.read_text(encoding="utf-8")
    conftest.write_text(conftest.read_text(encoding="utf-8").replace(fix, 'for name in ("tools",)]'), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(stale)}
    done = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "smoke"],
                          cwd=tree, env=env, capture_output=True, text=True, timeout=120)
    found = (stale / "harness" / "__init__.py").resolve()
    assert done.returncode not in (0, 5), done.stdout[-2000:] + done.stderr[-2000:]
    assert f"harness imported from {found}, not from this tree's src/graphs" in done.stdout + done.stderr, done.stdout[-2000:]
