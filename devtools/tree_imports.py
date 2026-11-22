"""A test root's imports come from the tree it runs in, or the suite does not load.

A lane's task worktree links the shared `<repo>/.venv` (`harness.worktree.link_venv`), and that venv's editable finder
maps `agent_tools`, `harness`, `runner`, `graphs` and `core` to the main checkout. A suite whose path holds only the
tree's root would import those packages from there, and pass while the tree's own change broke them. The root suite
(tests/conftest.py) and the smoke suite (smoke/conftest.py) both call `use_this_tree` before importing any of them.
The component suites are covered by their pytest.ini `pythonpath`.

This module imports none of those packages: it runs before they may be imported.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Mapping
from pathlib import Path

# The tree's source roots, in src/tools/pytest.ini's order. `agent_tools` imports `core` and `harness`, so all three
# go ahead of the venv, not only the one a suite tests.
SOURCE_ROOTS = ("tools", "graphs", "cartridges")
# Each import package the editable install maps, and the folder under the tree it must come from.
PACKAGE_HOMES: Mapping[str, str] = {
    "agent_tools": "src/tools",
    "harness": "src/graphs",
    "runner": "src/graphs",
    "graphs": "src/graphs",
    "core": "src/cartridges",
}


def source_paths(root: Path, roots: tuple[str, ...] = SOURCE_ROOTS) -> list[str]:
    """The tree's source roots, as the entries to put ahead of everything else on `sys.path`."""
    return [str(root / "src" / name) for name in roots]


def strays(root: Path, found: Mapping[str, Path]) -> list[str]:
    """One line per package in `found` (name to the file it imported from) that is not under its home in `root`."""
    return [
        f"{name} imported from {path}, not from this tree's {PACKAGE_HOMES[name]}"
        for name, path in sorted(found.items())
        if not path.is_relative_to(root / PACKAGE_HOMES[name])
    ]


def _origin(name: str) -> Path:
    """Where `name` is (or would be) imported from; finding it runs none of its code, so a stray never half-loads."""
    spec = importlib.util.find_spec(name)
    return Path(spec.origin if spec is not None and spec.origin else f"<{name} not found>").resolve()


def use_this_tree(root: Path, roots: tuple[str, ...] = SOURCE_ROOTS) -> None:
    """Put `root`'s source roots first on `sys.path`, then refuse with RuntimeError if any package lands elsewhere.

    A package imported before this ran (a plugin, a stale checkout ahead on the path) is not re-imported: the guard
    is what catches it. Nothing is imported here; each package is only located.
    """
    root = root.resolve()
    sys.path[:0] = source_paths(root, roots)
    found = {name: _origin(name) for name in PACKAGE_HOMES}
    wrong = strays(root, found)
    if wrong:
        raise RuntimeError("; ".join(wrong))
