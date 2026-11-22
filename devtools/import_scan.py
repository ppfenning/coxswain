"""Every module name a source file can reach, for the root suite's import guards (test_import_direction,
test_remote_surface).

An `import` or `from` statement at any depth; the first argument of `import_module` or `__import__`
(`harness.check_deps` recognises those calls); and any string literal shaped like a dotted module path, which is how
`core.module_facade` and the cox lazy proxies name a module. For an f-string, its literal head is the name:
`f"agent_tools.{n}"` reaches `agent_tools.` and whatever follows.
"""

from __future__ import annotations

import ast
import re

from harness.check_deps import _is_dynamic_import

_DOTTED = re.compile(r"[A-Za-z_]\w*(?:\.\w+)+(?::\w+)?")
_HEAD = re.compile(r"[A-Za-z_]\w*(?:\.\w*)+")


def _head(node: ast.expr) -> str | None:
    """Pure. A string constant's value, or an f-string's literal head; None for anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        head = "".join(v.value for v in node.values[:1] if isinstance(v, ast.Constant) and isinstance(v.value, str))
        return head or None
    return None


def module_names(tree: ast.AST) -> list[tuple[int, str]]:
    """Pure. (line, name) for each module `tree` names. `from m import a` gives `m` and `m.a`, since `a` may be a
    submodule or a proxy of one; a `module:function` spec gives its module."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found += [(node.lineno, node.module), *((node.lineno, f"{node.module}.{a.name}") for a in node.names)]
        elif isinstance(node, ast.Call) and _is_dynamic_import(node.func) and node.args:
            if (head := _head(node.args[0])) is not None:
                found.append((node.lineno, head))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _DOTTED.fullmatch(node.value):
            found.append((node.lineno, node.value.split(":")[0]))
        elif isinstance(node, ast.JoinedStr) and (head := _head(node)) is not None and _HEAD.fullmatch(head):
            found.append((node.lineno, head))
    return found
