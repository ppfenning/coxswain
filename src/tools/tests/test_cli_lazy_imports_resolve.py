"""Every module the CLI imports lazily exists, and so does each handler it names.

`cox` defers its imports: a command row or `_LazyHandler` names `agent_tools.mod:func`, a `_LazyModule` names a module,
and a few handlers call `importlib.import_module` on a literal path. None of them is imported until the command runs,
so a misspelt or never-built module passes every test that does not run that command. `cox release deploy` shipped
that way, wired to an `agent_tools.release_deploy_edge` that never landed.

This reads those paths from the source and imports each for real. Nothing is mocked.
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "agent_tools"
LAZY_CALLS = frozenset({"_LazyHandler", "_LazyModule", "import_module", "Command"})
TARGET = re.compile(r"agent_tools(?:\.\w+)+(?::\w+)?")


def _called(node: ast.Call) -> str:
    return node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")


def lazy_targets(source: str) -> set[str]:
    """Pure: each `agent_tools.mod[:func]` string passed directly to a lazy import or a command row in `source`."""
    return {
        arg.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and _called(node) in LAZY_CALLS
        for arg in (*node.args, *(keyword.value for keyword in node.keywords))
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and TARGET.fullmatch(arg.value)
    }


def test_lazy_targets_reads_handlers_modules_and_literal_imports_only():
    source = (
        'a = _LazyHandler("agent_tools.cli_x:_run")\n'
        'b = _LazyModule("agent_tools.chair")\n'
        'c = importlib.import_module("agent_tools.edge")\n'
        'd = commands.Command("go", None, "help", (), "agent_tools.cli:_go", False, ())\n'
        'e = importlib.import_module(f"agent_tools.forge_{name}")\n'
        'f = print("agent_tools.not_lazy")\n'
    )
    assert lazy_targets(source) == {"agent_tools.cli_x:_run", "agent_tools.chair", "agent_tools.edge", "agent_tools.cli:_go"}


ALL_TARGETS = sorted(set().union(*(lazy_targets(path.read_text(encoding="utf-8")) for path in PACKAGE.glob("*.py"))))


def test_the_cli_names_lazy_targets():
    assert len(ALL_TARGETS) > 100, "the scan found the command table and the lazy modules"


@pytest.mark.parametrize("target", ALL_TARGETS)
def test_each_lazy_target_imports_and_names_a_real_attribute(target: str):
    module_path, _, attribute = target.partition(":")
    module = importlib.import_module(module_path)
    assert not attribute or hasattr(module, attribute), f"{module_path} has no {attribute}"
