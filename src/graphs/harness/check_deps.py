"""Import dependency map of the components and their test modules.

The pure part takes path to source text and returns a DepMap. The edge, `load_dep_map`, only reads files.
Imports are read from source and matched to files in the given components. Anything outside them is ignored.
"""

from __future__ import annotations

import ast
import os
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = frozenset({".venv", "target", "node_modules"})

# level, module, names imported from it
_Ref = tuple[int, str, tuple[str, ...]]


@dataclass(frozen=True)
class DepMap:
    """What the lane selector reads: component imports, per-test reach, and files it could not read."""

    components: Mapping[str, frozenset[str]]
    test_reach: Mapping[str, frozenset[str]]
    unresolved: frozenset[str]


def _component(path: str, component_dirs: Sequence[str]) -> str | None:
    """The longest component directory holding `path`."""
    held = [d for d in component_dirs if path.startswith(d.rstrip("/") + "/")]
    return max(held, key=len) if held else None


def _is_test(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py")


def _in_tests_dir(path: str) -> bool:
    return "tests" in path.split("/")[:-1]


def _module(path: str, component: str) -> tuple[str, bool]:
    """Dotted module name relative to its component, and whether the file is a package __init__."""
    parts = path[len(component.rstrip("/")) + 1 :].removesuffix(".py").split("/")
    is_pkg = parts[-1] == "__init__"
    return ".".join(parts[:-1] if is_pkg else parts), is_pkg


def _node_refs(node: ast.AST) -> tuple[tuple[_Ref, ...], bool]:
    """Imports one node makes, and whether it makes a dynamic import that cannot be resolved."""
    if isinstance(node, ast.Import):
        return tuple((0, a.name, ()) for a in node.names), False
    if isinstance(node, ast.ImportFrom):
        return ((node.level, node.module or "", tuple(a.name for a in node.names)),), False
    if isinstance(node, ast.Call) and _is_dynamic_import(node.func):
        arg = node.args[0] if node.args else None
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and not arg.value.startswith("."):
            return ((0, arg.value, ()),), False
        return (), True
    return (), False


def _is_dynamic_import(func: ast.expr) -> bool:
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return name in ("import_module", "__import__")


def _scan(source: str) -> tuple[tuple[_Ref, ...], bool] | None:
    """Imports of one source text, or None when it does not parse."""
    try:
        tree = ast.parse(source)
    except SyntaxError, ValueError, RecursionError:
        return None
    found = [_node_refs(n) for n in ast.walk(tree)]
    return tuple(r for refs, _ in found for r in refs), any(dyn for _, dyn in found)


def _absolute(ref: _Ref, module: str, is_pkg: bool) -> str | None:
    """The full dotted name a reference points at, or None when a relative one climbs out of its package."""
    level, name, _ = ref
    if level == 0:
        return name
    package = module.split(".") if is_pkg else module.split(".")[:-1]
    keep = len(package) - (level - 1)
    if keep < 0:
        return None
    return ".".join([*package[:keep], *([name] if name else [])])


def _candidates(ref: _Ref, full: str) -> tuple[str, ...]:
    """Every module an import runs: each parent package, the module, and any imported name that is a submodule."""
    parts = full.split(".") if full else []
    prefixes = tuple(".".join(parts[: i + 1]) for i in range(len(parts)))
    return (*prefixes, *(f"{full}.{n}" if full else n for n in ref[2]))


def build_dep_map(files: Mapping[str, str], component_dirs: Sequence[str]) -> DepMap:
    comp_of = {p: c for p in files if (c := _component(p, component_dirs)) is not None}
    names = {p: _module(p, comp_of[p]) for p in comp_of}
    owners: dict[str, frozenset[str]] = {}
    index: dict[str, tuple[str, ...]] = {}
    for path, (module, _) in names.items():
        top = module.split(".")[0]
        owners = {**owners, top: owners.get(top, frozenset()) | {comp_of[path]}}
        # a test sibling is imported by bare name, since each suite runs from its own folder
        rel = path[len(comp_of[path].rstrip("/")) + 1 :].split("/")
        aliases = [rel[1].removesuffix(".py")] if len(rel) == 2 and rel[0] == "tests" else []
        for name in [module, *aliases]:
            index = {**index, name: (*index.get(name, ()), path)}

    scans = {p: _scan(files[p]) for p in comp_of}
    unresolved = frozenset(p for p, s in scans.items() if s is None or s[1])

    edges: dict[str, frozenset[str]] = {}
    imported: dict[str, frozenset[str]] = {c: frozenset() for c in component_dirs}
    for path, scan in scans.items():
        comp = comp_of[path]
        module, is_pkg = names[path]
        refs = scan[0] if scan is not None else ()
        targets: set[str] = set()
        for ref in refs:
            full = _absolute(ref, module, is_pkg)
            if full is None:
                continue
            for name in _candidates(ref, full):
                found = index.get(name, ())
                targets |= set([t for t in found if comp_of[t] == comp] or found)
            top = full.split(".")[0]
            if ref[0] == 0 and comp not in owners.get(top, frozenset()):
                imported = {**imported, comp: imported[comp] | owners.get(top, frozenset())}
        edges = {**edges, path: frozenset(targets - {path})}

    reach = {p: _closure(p, edges) for p in edges if _is_test(p)}
    return DepMap(
        components={c: frozenset(deps - {c}) for c, deps in imported.items()},
        test_reach={p: frozenset(f for f in r if not _is_test(f) and not _in_tests_dir(f)) for p, r in reach.items()},
        unresolved=unresolved,
    )


def _closure(start: str, edges: Mapping[str, frozenset[str]]) -> frozenset[str]:
    """Every file reachable from `start`, following imports through any file and any cycle."""
    seen: frozenset[str] = frozenset()
    todo = [start]
    while todo:
        here = todo.pop()
        if here in seen:
            continue
        seen = seen | {here}
        todo.extend(edges.get(here, ()))
    return seen - {start}


def dependents(dep_map: DepMap, changed_paths: Collection[str], component_dirs: Sequence[str]) -> frozenset[str]:
    """The components owning a changed path, plus every component that imports one of them."""
    owning = frozenset(c for p in changed_paths if (c := _component(p, component_dirs)) is not None)
    importers = frozenset(c for c, deps in dep_map.components.items() if deps & owning)
    return owning | importers


def read_sources(worktree: Path, component_dirs: Sequence[str]) -> dict[str, str]:
    """Edge: repo-relative posix path to text for every .py file under the components."""
    found: dict[str, str] = {}
    for component in component_dirs:
        for root, dirs, names in os.walk(worktree / component):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in names:
                if name.endswith(".py"):
                    path = Path(root, name)
                    found[path.relative_to(worktree).as_posix()] = path.read_text(encoding="utf-8", errors="replace")
    return found


def load_dep_map(worktree: Path, component_dirs: Sequence[str]) -> DepMap:
    return build_dep_map(read_sources(worktree, component_dirs), component_dirs)
