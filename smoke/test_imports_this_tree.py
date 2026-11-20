"""The suite's in-process imports come from this tree, not from the checkout the runner's venv installed.

A chair lane's venv can carry an editable install of an older checkout. `smoke/conftest.py` imports the forge's parser
from `agent_tools`, which imports `core` and `harness`; if only `src/tools` is put ahead on `sys.path`, those two still
resolve through the venv, and the suite fails to load on a name this tree has but that checkout lacks.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = {"agent_tools": "tools", "harness": "graphs", "core": "cartridges"}


@pytest.mark.parametrize("name", sorted(SOURCE_ROOT))
def test_the_source_packages_import_from_this_tree(name: str) -> None:
    module = importlib.import_module(name)
    where = ROOT / "src" / SOURCE_ROOT[name]
    assert Path(module.__file__).resolve().is_relative_to(where), f"{name} imported from {module.__file__}, not {where}"


def test_a_stale_checkout_ahead_on_the_path_does_not_shadow_this_tree(tmp_path: Path) -> None:
    """The lane's failure, rebuilt: an older checkout's packages sit ahead of this tree on the path, and its `core`
    has a `jsontypes` without `conform`, as the stale checkout's did. The suite still loads and imports all three
    packages from this tree."""
    stale = tmp_path / "stale-checkout"
    for name in SOURCE_ROOT:
        (stale / name).mkdir(parents=True)
        (stale / name / "__init__.py").write_text(f'"""A stale checkout\'s {name}."""\n')
    (stale / "core" / "jsontypes.py").write_text('"""A stale checkout\'s jsontypes: no `conform`."""\n')
    inherited = os.environ.get("PYTHONPATH", "")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in (str(stale), inherited) if p)}
    node = f"{Path(__file__).relative_to(ROOT)}::test_the_source_packages_import_from_this_tree"
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", node],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
