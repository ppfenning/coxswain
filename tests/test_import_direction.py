"""F3: the harness, the graphs, the runner and core never reach the tools package, not even inside a function.

The tools sit on top: they import the harness, and `shell.py` (the composition root) passes the harness what it needs
from them (`harness.decompose_ports`). A reach the other way is a cycle hidden by laziness, and it breaks only when
the line runs. `devtools.import_scan` sees an import at any depth, an `import_module` or `__import__` of a literal or
f-string name, and a string literal naming an `agent_tools.` module, as `core.module_facade` specs do.
"""

from __future__ import annotations

import ast
from pathlib import Path

from devtools.import_scan import module_names

ROOT = Path(__file__).resolve().parent.parent
LOWER = ("src/graphs/harness", "src/graphs/graphs", "src/graphs/runner", "src/cartridges/core")
UPPER = "agent_tools"

# "path:line" -> why that reach cannot move yet. Keep it short; empty is the goal.
ALLOWED: dict[str, str] = {
    "src/cartridges/core/generated_files.py:32": "the README generator's `python -m` argv, run by the lander in a"
    " subprocess of the tools; data, never imported",
}


def reaches_upper(name: str) -> bool:
    return name.split(".")[0] == UPPER


def upward_reaches() -> list[str]:
    """Each "path:line name" under LOWER that reaches UPPER, tests excluded."""
    return [
        f"{path.relative_to(ROOT).as_posix()}:{line} {name}"
        for package in LOWER
        for path in sorted((ROOT / package).rglob("*.py"))
        if "tests" not in path.relative_to(ROOT / package).parts
        for line, name in module_names(ast.parse(path.read_text(encoding="utf-8")))
        if reaches_upper(name)
    ]


def test_the_lower_packages_never_reach_the_tools() -> None:
    assert [site for site in upward_reaches() if site.split(" ")[0] not in ALLOWED] == []


def test_every_allowed_site_still_reaches_the_tools() -> None:
    sites = {site.split(" ")[0] for site in upward_reaches()}
    assert [site for site in ALLOWED if site not in sites] == []


def test_the_scan_sees_each_way_a_module_reaches_the_tools() -> None:
    source = "\n".join([
        "def f():",
        "    from agent_tools import route",
        "importlib.import_module('agent_tools.land')",
        "importlib.import_module(f'agent_tools.{n}')",
        "__import__('agent_tools.route')",
        "module_facade.install(__name__, ('agent_tools.cli_route',))",
        "handler = 'agent_tools.inbox_cli:handler'",
        "import_module(package)",
        "text = 'agent tools are fine in prose'",
    ])
    lines = sorted({line for line, name in module_names(ast.parse(source)) if reaches_upper(name)})
    assert lines == [2, 3, 4, 5, 6, 7]
