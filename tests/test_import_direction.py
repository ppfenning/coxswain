"""F3: the harness, the graphs and the runner never import the tools package, not even inside a function.

The tools sit on top: they import the harness, and `shell.py` (the composition root) passes the harness what it needs
from them (`harness.decompose_ports`). An import the other way is a cycle hidden by laziness, and it breaks only when
the line runs. The scan is an AST walk, so a function-local import, a `TYPE_CHECKING` import and an
`importlib.import_module("agent_tools...")` with a literal name all count.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOWER = ("src/graphs/harness", "src/graphs/graphs", "src/graphs/runner")
UPPER = "agent_tools"

# "path:line" -> why that import cannot move yet. Keep it short; empty is the goal.
ALLOWED: dict[str, str] = {}


def imported_names(tree: ast.AST) -> list[tuple[int, str]]:
    """Pure. (line, module) for every import in `tree`, at any depth, and every `import_module` of a literal name."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.lineno, node.module))
        elif (
            isinstance(node, ast.Call)
            and (node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")) == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.append((node.lineno, node.args[0].value))
    return found


def upward_imports() -> list[str]:
    """Each "path:line module" under LOWER that imports UPPER, tests excluded."""
    return [
        f"{path.relative_to(ROOT).as_posix()}:{line} {module}"
        for package in LOWER
        for path in sorted((ROOT / package).rglob("*.py"))
        if "tests" not in path.relative_to(ROOT / package).parts
        for line, module in imported_names(ast.parse(path.read_text(encoding="utf-8")))
        if module.split(".")[0] == UPPER
    ]


def test_the_harness_graphs_and_runner_never_import_the_tools() -> None:
    assert [site for site in upward_imports() if site.split(" ")[0] not in ALLOWED] == []


def test_every_allowed_site_still_imports_the_tools() -> None:
    sites = {site.split(" ")[0] for site in upward_imports()}
    assert [site for site in ALLOWED if site not in sites] == []


def test_the_scan_sees_a_function_local_and_a_literal_dynamic_import() -> None:
    source = "def f():\n    from agent_tools import route\n\nimportlib.import_module('agent_tools.land')\n"
    assert sorted(imported_names(ast.parse(source))) == [(2, "agent_tools"), (4, "agent_tools.land")]
