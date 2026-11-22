"""The root suite imports the tree it runs in, never the checkout the venv's editable install maps (devtools.tree_imports).

A lane's task worktree links the shared venv, whose editable finder points at the main checkout. These tests build that
case for real: a copy of this tree, run under this interpreter, whose venv maps every package somewhere else.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from devtools import tree_imports

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    "agent_tools": "src/tools",
    "harness": "src/graphs",
    "runner": "src/graphs",
    "graphs": "src/graphs",
    "core": "src/cartridges",
    "providers": "src/cartridges",
}


def _ignore(folder: str, names: list[str]) -> set[str]:
    return {name for name in names if name in {"tests", "__pycache__"}}


def _copy_tree(tree: Path) -> None:
    """The root suite's inputs, copied: its pyproject, conftest and devtools, and the packages it imports."""
    for name, home in PACKAGES.items():
        shutil.copytree(ROOT / home / name, tree / home / name, ignore=_ignore)
    shutil.copytree(ROOT / "coxswain", tree / "coxswain", ignore=_ignore)
    shutil.copytree(ROOT / "devtools", tree / "devtools", ignore=_ignore)
    (tree / "tests").mkdir()
    shutil.copy2(ROOT / "tests" / "conftest.py", tree / "tests" / "conftest.py")
    shutil.copy2(ROOT / "pyproject.toml", tree / "pyproject.toml")


def _run_root_suite(tree: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist", "tests"],
                          cwd=tree, env=env, capture_output=True, text=True, timeout=300)


def test_strays_names_each_package_outside_its_home_in_the_tree(tmp_path: Path) -> None:
    tree, main = tmp_path / "tree", tmp_path / "main"
    found = {
        "agent_tools": tree / "src" / "tools" / "agent_tools" / "__init__.py",
        "harness": main / "src" / "graphs" / "harness" / "__init__.py",
        "core": tree / "src" / "graphs" / "core" / "__init__.py",
    }
    assert tree_imports.strays(tree, found) == [
        f"core imported from {found['core']}, not from this tree's src/cartridges",
        f"harness imported from {found['harness']}, not from this tree's src/graphs",
    ]
    assert tree_imports.strays(tree, {"agent_tools": found["agent_tools"]}) == []


def test_the_root_suite_imports_the_tree_its_run_in_and_not_the_venvs_checkout(tmp_path: Path) -> None:
    """A copy of this tree whose `core` carries a mark the venv's checkout does not have: the root conftest puts the
    copy's src/ first, so a test there sees the mark, and every mapped package comes from the copy."""
    tree = tmp_path / "tree"
    _copy_tree(tree)
    mark = uuid.uuid4().hex
    with (tree / "src" / "cartridges" / "core" / "__init__.py").open("a", encoding="utf-8") as init:
        init.write(f"\nTREE_MARK = {mark!r}\n")
    (tree / "tests" / "test_probe.py").write_text(
        "from pathlib import Path\n\nimport agent_tools, core, graphs, harness, runner\n\n\n"
        "def test_probe():\n"
        f"    assert getattr(core, 'TREE_MARK', None) == {mark!r}\n"
        "    tree = Path(__file__).resolve().parents[1]\n"
        "    for package in (agent_tools, core, graphs, harness, runner):\n"
        "        assert Path(package.__file__).resolve().is_relative_to(tree / 'src'), package.__file__\n",
        encoding="utf-8",
    )
    done = _run_root_suite(tree)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    assert "1 passed" in done.stdout


def test_the_root_conftest_refuses_a_tree_whose_path_fix_leaves_harness_to_the_venv(tmp_path: Path) -> None:
    """The same copy with the path fix cut back to src/tools (the smoke conftest's first fix): `harness` then resolves
    through the venv's editable finder, outside the copy, and the root conftest refuses to load."""
    tree = tmp_path / "tree"
    _copy_tree(tree)
    fixer = tree / "devtools" / "tree_imports.py"
    fix = 'SOURCE_ROOTS = ("tools", "graphs", "cartridges")'
    assert fix in fixer.read_text(encoding="utf-8")
    fixer.write_text(fixer.read_text(encoding="utf-8").replace(fix, 'SOURCE_ROOTS = ("tools",)'), encoding="utf-8")
    (tree / "tests" / "test_probe.py").write_text("def test_probe():\n    pass\n", encoding="utf-8")
    done = _run_root_suite(tree)
    assert done.returncode not in (0, 5), done.stdout[-3000:] + done.stderr[-3000:]
    assert "harness imported from" in done.stdout + done.stderr, done.stdout[-3000:] + done.stderr[-3000:]
    assert "not from this tree's src/graphs" in done.stdout + done.stderr
