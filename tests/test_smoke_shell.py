"""The smoke suite's shell, where it needs no sandbox: no Postgres, no install, no `cox`.

`gh_replay.real_gh` resolving a mise shim (moved from smoke/test_contract_gh.py, which keeps the tests that run the
real `gh`), and the conftest's refusal to load packages from anywhere but this tree (the guard that replaced
smoke/test_imports_this_tree.py). Real files and real processes; nothing is replaced.
"""

from __future__ import annotations

import os
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


def test_the_committed_timings_file_reads_and_names_only_smoke_test_files():
    timings = plan.read_timings((ROOT / "smoke" / plan.TIMINGS_FILE).read_text(encoding="utf-8"))
    assert timings and all(name.startswith("smoke/test_") and name.endswith(".py") for name in timings)


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
