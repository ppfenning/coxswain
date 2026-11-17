"""Unwired code is a finding (docs/hardening.md, H10 item 5).

Lists the public functions, classes and modules a diff ADDS under src/ and fails when one has no reference
outside tests. A helper that only its own unit test calls does not run, however well that test passes.

How it decides:
  added      a top-level `def`, `async def` or `class` whose name does not start with `_`, present in the new
             source of a changed non-test .py file under src/ and absent from its source at the merge base;
             and a whole new non-test .py file under src/ whose name does not start with `_` (the module).
  reference  in any tracked or untracked non-test file other than tests: a loaded name, an attribute, an import,
             or a string that is a dotted path (`pkg.mod`, `pkg.mod:func`, as lazy tables and pyproject entry points
             write them); in .toml/.yaml/.yml/.json/.cfg/.ini/.sh files, a dotted path's parts. The symbol's own body
             does not count (recursion is not a caller), a module's own file does not count, and neither does an
             `__all__` list or an `__init__.py` re-export.
  tests      a path with a `tests`, `test`, `smoke` or `fixtures` folder, or a `test_*.py`, `*_test.py` or
             `conftest.py` file.
Matching is by name, so a new symbol whose name already appears elsewhere passes: the check errs toward silence.

An entry point a name cannot reach (a command looked up by a computed name, a plugin hook) goes in the allowlist,
one `path::name  # reason` per line; the reason is required, and an entry whose path no longer exists is refused.

Usage: python .github/check_unwired.py [--base REF] [--root DIR] [--allow FILE]. Stdlib only.
The base defaults to origin/main; the diff runs from its merge base with HEAD to the working tree, plus untracked files.
Exits 0 when every added symbol is referenced, 1 when one is not, 2 when git or the allowlist is unusable.
"""

import argparse
import ast
import pathlib
import re
import subprocess
import sys
import warnings
from collections.abc import Iterable, Mapping, Sequence
from typing import NamedTuple

GIT_TIMEOUT = 60
DEFAULT_ALLOW = ".github/unwired-allow.txt"
TEST_DIRS = frozenset({"tests", "test", "smoke", "fixtures"})
TEXT_SUFFIXES = frozenset({".toml", ".yaml", ".yml", ".json", ".cfg", ".ini", ".sh"})
DOTTED = re.compile(r"[A-Za-z_]\w*(?:[.:][A-Za-z_]\w*)+")
REF_STRING = re.compile(r"[A-Za-z_][\w.]*(?::[A-Za-z_]\w*)?")


class Change(NamedTuple):
    status: str
    old: str | None
    new: str


class Symbol(NamedTuple):
    path: str
    name: str
    kind: str
    line: int


class Allowlist(NamedTuple):
    entries: frozenset[tuple[str, str]]
    errors: tuple[str, ...]


# ── pure core ────────────────────────────────────────────────────────────────


def parse_name_status(text: str) -> tuple[Change, ...]:
    """`git diff --name-status -z -M` output; a rename or copy carries two paths, every other status one."""
    fields = [field for field in text.split("\0") if field]
    changes: list[Change] = []
    index = 0
    while index < len(fields):
        status = fields[index][0]
        if status in "RC":
            changes.append(Change(status, fields[index + 1], fields[index + 2]))
            index += 3
        else:
            path = fields[index + 1]
            changes.append(Change(status, None if status == "A" else path, path))
            index += 2
    return tuple(changes)


def is_test_path(path: str) -> bool:
    parts = pathlib.PurePosixPath(path).parts
    name = parts[-1] if parts else ""
    return (
        any(part in TEST_DIRS for part in parts[:-1])
        or name == "conftest.py"
        or (name.startswith("test_") and name.endswith(".py"))
        or name.endswith("_test.py")
    )


def in_scope(path: str) -> bool:
    return path.startswith("src/") and path.endswith(".py") and not is_test_path(path)


def public_symbols(path: str, source: str) -> tuple[Symbol, ...]:
    kinds = {ast.FunctionDef: "function", ast.AsyncFunctionDef: "function", ast.ClassDef: "class"}
    return tuple(
        Symbol(path, node.name, kinds[type(node)], node.lineno)
        for node in ast.parse(source, filename=path).body
        if type(node) in kinds and not node.name.startswith("_")
    )


def module_symbol(path: str) -> Symbol | None:
    stem = pathlib.PurePosixPath(path).stem
    return None if stem.startswith("_") else Symbol(path, stem, "module", 1)


def added_symbols(changes: Iterable[Change], old: Mapping[str, str], new: Mapping[str, str]) -> tuple[Symbol, ...]:
    """Symbols in scope that the new sources define and the old ones (keyed by the change's old path) do not."""
    added: list[Symbol] = []
    for change in changes:
        if change.status == "D" or not in_scope(change.new):
            continue
        before = old.get(change.old) if change.old and in_scope(change.old) else None
        if before is None:
            module = module_symbol(change.new)
            added.extend([module] if module else [])
        known = {symbol.name for symbol in public_symbols(change.old or "", before)} if before is not None else set()
        added.extend(symbol for symbol in public_symbols(change.new, new[change.new]) if symbol.name not in known)
    return tuple(added)


def _string_names(value: str) -> set[str]:
    if not REF_STRING.fullmatch(value):
        return set()
    return set(re.split(r"[.:]", value))


def _is_dunder_all(node: ast.AST) -> bool:
    targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
    return isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)) and any(
        isinstance(target, ast.Name) and target.id == "__all__" for target in targets
    )


def python_references(path: str, source: str, skip_def: str | None = None) -> frozenset[str]:
    """The names a Python file refers to, leaving out the top-level `def`/`class` named `skip_def`."""
    reexport = pathlib.PurePosixPath(path).name == "__init__.py"
    names: set[str] = set()
    pending: list[ast.AST] = [
        node
        for node in ast.parse(source, filename=path).body
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == skip_def)
        and not _is_dunder_all(node)
    ]
    while pending:
        node = pending.pop()
        pending.extend(ast.iter_child_nodes(node))
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names |= _string_names(node.value)
        elif isinstance(node, ast.Import) and not reexport:
            names |= {part for alias in node.names for part in alias.name.split(".")}
        elif isinstance(node, ast.ImportFrom) and not reexport:
            names |= set((node.module or "").split(".")) | {alias.name for alias in node.names}
    names.discard("")
    return frozenset(names)


def text_references(source: str) -> frozenset[str]:
    """The parts of every dotted path in a config or script file: `pkg.mod:func` names pkg, mod and func."""
    return frozenset(part for match in DOTTED.findall(source) for part in re.split(r"[.:]", match))


def references(path: str, source: str, skip_def: str | None = None) -> frozenset[str]:
    if path.endswith(".py"):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                return python_references(path, source, skip_def)
        except SyntaxError:
            return frozenset()
    if pathlib.PurePosixPath(path).suffix in TEXT_SUFFIXES:
        return text_references(source)
    return frozenset()


def is_referenced(
    symbol: Symbol, sources: Mapping[str, str], seen: dict[tuple[str, str | None], frozenset[str]] | None = None
) -> bool:
    """Whether any non-test file other than the symbol's own body refers to its name; `seen` memoises the parses."""
    seen = {} if seen is None else seen
    for path, source in sources.items():
        if is_test_path(path) or symbol.name not in source or (path == symbol.path and symbol.kind == "module"):
            continue
        key = (path, symbol.name if path == symbol.path else None)
        if key not in seen:
            seen[key] = references(path, source, skip_def=key[1])
        if symbol.name in seen[key]:
            return True
    return False


def parse_allowlist(text: str, existing: Iterable[str]) -> Allowlist:
    present = set(existing)
    entries: set[tuple[str, str]] = set()
    errors: list[str] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        entry, _, reason = line.partition("#")
        path, sep, name = entry.strip().partition("::")
        if not sep or not path or not name.strip():
            errors.append(f"allowlist line {number}: expected `path::name  # reason`, got {raw!r}")
        elif not reason.strip():
            errors.append(f"allowlist line {number}: {entry.strip()} has no reason; say why nothing calls it by name")
        elif path not in present:
            errors.append(f"allowlist line {number}: {path} does not exist; drop the entry")
        else:
            entries.add((path, name.strip()))
    return Allowlist(frozenset(entries), tuple(errors))


def unwired(added: Sequence[Symbol], sources: Mapping[str, str], allow: frozenset[tuple[str, str]]) -> tuple[Symbol, ...]:
    seen: dict[tuple[str, str | None], frozenset[str]] = {}
    return tuple(
        symbol
        for symbol in added
        if (symbol.path, symbol.name) not in allow and not is_referenced(symbol, sources, seen)
    )


def report(added: Sequence[Symbol], found: Sequence[Symbol]) -> tuple[str, ...]:
    if not found:
        return (f"unwired: {len(added)} added public symbol(s) under src/, each referenced outside tests",)
    return tuple(
        f"{symbol.path}:{symbol.line}: unwired: {symbol.kind} {symbol.name} has no reference outside tests;"
        " call it from the code that should run it, or allowlist an entry point with its reason"
        for symbol in found
    )


# ── glue ─────────────────────────────────────────────────────────────────────


class GitError(Exception):
    pass


def _git(root: pathlib.Path, *args: str) -> str:
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitError(f"git {' '.join(args)}: {error}") from error
    if done.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {done.stderr.strip() or f'exit {done.returncode}'}")
    return done.stdout


def _read(root: pathlib.Path, path: str) -> str | None:
    try:
        return (root / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Fail when a diff adds a public symbol under src/ that nothing calls.")
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--root", default=".")
    parser.add_argument("--allow", default=DEFAULT_ALLOW)
    args = parser.parse_args(argv)
    try:
        root = pathlib.Path(_git(pathlib.Path(args.root), "rev-parse", "--show-toplevel").strip())
        base = _git(root, "merge-base", args.base, "HEAD").strip()
        changes = parse_name_status(_git(root, "diff", "--name-status", "-z", "-M", base, "--", "src"))
        untracked = [p for p in _git(root, "ls-files", "-z", "--others", "--exclude-standard").split("\0") if p]
        changes += tuple(Change("A", None, path) for path in untracked if path.startswith("src/"))
        tracked = [p for p in _git(root, "ls-files", "-z").split("\0") if p]
        old = {
            change.old: _git(root, "show", f"{base}:{change.old}")
            for change in changes
            if change.old and change.status != "D" and in_scope(change.old) and in_scope(change.new)
        }
    except GitError as error:
        print(f"unwired: cannot read the diff: {error}", file=sys.stderr)
        return 2
    paths = sorted(set(tracked) | set(untracked))
    allow_text = _read(root, args.allow) or ""
    allow = parse_allowlist(allow_text, paths)
    if allow.errors:
        print("\n".join(allow.errors), file=sys.stderr)
        return 2
    new = {change.new: _read(root, change.new) or "" for change in changes if change.status != "D"}
    try:
        added = added_symbols(changes, old, new)
    except SyntaxError as error:
        print(f"unwired: cannot parse {error.filename}: {error.msg}", file=sys.stderr)
        return 2
    readable = (path for path in paths if path.endswith(".py") or pathlib.PurePosixPath(path).suffix in TEXT_SUFFIXES)
    sources = {path: text for path in readable if not is_test_path(path) and (text := _read(root, path)) is not None}
    found = unwired(added, sources, allow.entries)
    print("\n".join(report(added, found)))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
