"""references_problems — files outside a task's surfaces that use what the task defines or changes.

Pure: tasks, the repository tree and a path-to-text mapping in, `Problem` entries out. Nothing here reads a file.
`texts` holds the current text of each surface file and of each candidate referrer, loaded once at the edge.

Python symbols are top-level `def` and `class` names, and referrers are found from the parsed tree: imports from, calls
by bare name, and attribute uses. SQL files yield table names from `create table` statements only, matched against
`from`, `join`, `into`, `update` and `table` clauses in other SQL files and in string literals of test modules.
Every other language yields nothing rather than a substring match.

A change is non-additive when the task body has a sentence naming a symbol in backticks beside a word such as
remove, delete, rename, drop, signature, parameter or argument. A referrer owned by a task that is not in the changed
task's `needs` closure breaks the build until that task runs.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping, Sequence
from typing import Any

from graphs.delivery.ticket_lint import Problem

__all__ = ["references_problems"]

_NEW_SUFFIX = " (new)"
_CLAIM = re.compile(r"\b(?:remov\w*|delet\w*|renam\w*|drop\w*|signature|parameter\w*|argument\w*)\b", re.IGNORECASE)
_TICKED = re.compile(r"`([A-Za-z_]\w*)[^`]*`")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n")
_CREATE_TABLE = re.compile(
    r"\bcreate\s+(?:or\s+replace\s+)?table\s+(?:if\s+not\s+exists\s+)?(?:\w+\.)?[\"`]?(\w+)", re.IGNORECASE
)
_SQL_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_SQL_LINE_COMMENT = re.compile(r"--[^\n]*")


def _surface_paths(task: Mapping[str, Any]) -> list[str]:
    return [str(s).removesuffix(_NEW_SUFFIX) for s in task.get("surfaces") or []]


def _parse(text: str) -> ast.Module | None:
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


def _strip_sql_comments(text: str) -> str:
    return _SQL_LINE_COMMENT.sub("", _SQL_BLOCK_COMMENT.sub(" ", text))


def _defined_symbols(path: str, text: str) -> frozenset[str]:
    if path.endswith(".py"):
        module = _parse(text)
        defs = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        return frozenset(n.name for n in module.body if isinstance(n, defs)) if module else frozenset()
    if path.endswith(".sql"):
        return frozenset(_CREATE_TABLE.findall(_strip_sql_comments(text)))
    return frozenset()


def _claimed(body: str, symbols: frozenset[str]) -> frozenset[str]:
    """Symbols the body says are removed or have a changed parameter list."""
    sentences = [s for s in _SENTENCE_BREAK.split(body) if _CLAIM.search(s)]
    return frozenset(name for s in sentences for name in _TICKED.findall(s) if name in symbols)


def _used_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.ImportFrom):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return [node.func.id]
    if isinstance(node, ast.Attribute):
        return [node.attr]
    return []


def _python_refs(text: str, symbols: frozenset[str]) -> frozenset[str]:
    module = _parse(text)
    used = {name for node in ast.walk(module) for name in _used_names(node)} if module else set()
    return symbols & used


def _sql_haystacks(path: str, text: str) -> list[str]:
    if path.endswith(".sql"):
        return [_strip_sql_comments(text)]
    module = _parse(text) if path.endswith(".py") and "test" in path.lower() else None
    if module is None:
        return []
    return [n.value for n in ast.walk(module) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _sql_refs(path: str, text: str, tables: frozenset[str]) -> frozenset[str]:
    haystacks = _sql_haystacks(path, text)

    def named(table: str) -> bool:
        clause = re.compile(rf"\b(?:from|join|into|update|table)\s+(?:\w+\.)?[\"`]?{re.escape(table)}\b", re.IGNORECASE)
        return any(clause.search(h) for h in haystacks)

    return frozenset(t for t in tables if named(t))


def _referenced(surface: str, symbols: frozenset[str], path: str, text: str) -> frozenset[str]:
    if surface.endswith(".py"):
        return _python_refs(text, symbols) if path.endswith(".py") else frozenset()
    return _sql_refs(path, text, symbols)


def _owners(tasks: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    owners: dict[str, list[str]] = {}
    for task in tasks:
        for path in _surface_paths(task):
            owners.setdefault(path, []).append(str(task["id"]))
    return owners


def _needs_closure(task_id: str, needs_of: Mapping[str, Sequence[str]]) -> frozenset[str]:
    seen: set[str] = set()
    pending = list(needs_of.get(task_id, ()))
    while pending:
        current = pending.pop()
        if current not in seen:
            seen.add(current)
            pending.extend(needs_of.get(current, ()))
    return frozenset(seen)


def _problem(
    task_id: str, surface: str, path: str, names: frozenset[str], owner: str | None, breaking: frozenset[str]
) -> Problem:
    detail = f"{path} references {', '.join(sorted(names))} from {surface} (owner: {owner or 'none'})"
    if breaking:
        return Problem(
            task_id,
            "references",
            f"{detail}; the non-additive change to {', '.join(sorted(breaking))} breaks the build until {owner} runs",
            f"add {owner} to this task's needs, or move {path} into this task's surfaces",
        )
    return Problem(
        task_id,
        "references",
        detail,
        f"name {path} in a task's surfaces, or confirm the change leaves it working",
    )


def _surface_problems(
    task: Mapping[str, Any],
    surface: str,
    symbols: frozenset[str],
    candidates: Mapping[str, str],
    owners: Mapping[str, Sequence[str]],
    earlier: frozenset[str],
) -> list[Problem]:
    task_id = str(task["id"])
    non_additive = _claimed(str(task.get("body") or ""), symbols)
    problems: list[Problem] = []
    for path, text in sorted(candidates.items()):
        names = _referenced(surface, symbols, path, text)
        if not names:
            continue
        others = [o for o in owners.get(path, ()) if o != task_id]
        later = [o for o in others if o not in earlier]
        owner = (later or others or [None])[0]
        breaking = (names & non_additive) if later else frozenset()
        problems.append(_problem(task_id, surface, path, names, owner, breaking))
    return problems


def references_problems(
    tasks: Sequence[Mapping[str, Any]],
    tree: Sequence[Mapping[str, Any]],
    texts: Mapping[str, str],
) -> list[Problem]:
    known = {str(row.get("path")) for row in tree}
    owners = _owners(tasks)
    needs_of = {str(t["id"]): [str(n) for n in t.get("needs") or []] for t in tasks}
    problems: list[Problem] = []
    for task in tasks:
        surfaces = _surface_paths(task)
        candidates = {p: t for p, t in texts.items() if p not in surfaces and (p in known or p in owners)}
        earlier = _needs_closure(str(task["id"]), needs_of)
        for surface in surfaces:
            symbols = _defined_symbols(surface, texts.get(surface, ""))
            if symbols:
                problems.extend(_surface_problems(task, surface, symbols, candidates, owners, earlier))
    return problems
