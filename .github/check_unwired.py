"""Unwired code is a finding (docs/hardening.md, H10 item 5).

Lists the public functions, classes and modules a diff ADDS under src/ and fails when one has no caller outside
tests. A helper that only its own unit test calls does not run, however well that test passes.

How it decides:
  added      a top-level `def`, `async def` or `class` whose name does not start with `_`, present in the new
             source of a changed non-test .py file under src/ and absent from its source at the merge base;
             and a whole new non-test .py file under src/ whose name does not start with `_` (the module).
  reference  the symbol's name, used in a non-test file on a line the diff adds or changes, or anywhere in its own
             file outside its own body (recursion is not a caller). A use is a loaded name, an attribute, an import,
             a string holding a dotted path (`pkg.mod`, `pkg.mod:func`, as lazy tables and pyproject entry points
             write them), or a bare-name string passed to `getattr` or `hasattr`; in .toml/.yaml/.yml/.json/.cfg/.ini/
             .sh files, a part of a dotted path. A module's own file, an `__all__` list and an `__init__.py` re-export
             are not callers. A use inside a private top-level function or class the diff also adds counts only when
             that private one is itself referenced, so a new `_wrapper` nothing calls does not wire what it calls.
  tests      a path with a `tests`, `test`, `smoke` or `fixtures` folder, or a `test_*.py`, `*_test.py` or
             `conftest.py` file.
Matching is by name on the lines the diff touches, so it errs toward silence only when the diff itself uses the name.
It catches a symbol nothing calls; it does not catch a caller that passes the wrong arguments, or none of the ones
that matter (a guard called without the paths it should check). That is the smoke suite's to prove.

An entry point a name cannot reach (a command looked up by a computed name, a plugin hook) goes in the allowlist,
one `path::name  # reason` per line; the reason is required, and an entry whose path or name no longer exists is
refused.

Usage: python .github/check_unwired.py [--base REF] [--root DIR] [--allow FILE]. Stdlib only.
The base is --base, else $COX_LANE_BASE (an epic lane's phase branch, exported by the harness), else origin/main.
The diff runs from the base's merge base with HEAD to the working tree, plus untracked files.
Exits 0 when every added symbol is referenced, 1 when one is not, 2 when git or the allowlist is unusable.
"""

import argparse
import ast
import os
import pathlib
import re
import subprocess
import sys
import warnings
from collections.abc import Iterable, Mapping, Sequence
from typing import NamedTuple

GIT_TIMEOUT = 60
DEFAULT_ALLOW = ".github/unwired-allow.txt"
DEFAULT_BASE = "origin/main"
LANE_BASE_VAR = "COX_LANE_BASE"
TEST_DIRS = frozenset({"tests", "test", "smoke", "fixtures"})
TEXT_SUFFIXES = frozenset({".toml", ".yaml", ".yml", ".json", ".cfg", ".ini", ".sh"})
NAME_LOOKUPS = frozenset({"getattr", "hasattr"})
DOTTED = re.compile(r"[A-Za-z_]\w*(?:[.:][A-Za-z_]\w*)+")
IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


class Change(NamedTuple):
    status: str
    old: str | None
    new: str


class Symbol(NamedTuple):
    path: str
    name: str
    kind: str
    line: int


class Ref(NamedTuple):
    """One use of a name: the line it is on, and the top-level `def`/`class` it sits in (None at module level)."""

    name: str
    line: int
    owner: str | None


class Allowlist(NamedTuple):
    entries: frozenset[tuple[str, str]]
    errors: tuple[str, ...]


# ── pure core ────────────────────────────────────────────────────────────────


def default_base(environ: Mapping[str, str]) -> str:
    return environ.get(LANE_BASE_VAR) or DEFAULT_BASE


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


def parse_added_lines(diff: str) -> dict[str, frozenset[int]]:
    """New-side line numbers each file's hunks add or change, from `git diff -U0 --dst-prefix=b/` output."""
    added: dict[str, set[int]] = {}
    path: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            path = target[2:] if target.startswith("b/") else None
            if path is not None:
                added.setdefault(path, set())
        elif path is not None and (hunk := HUNK.match(line)):
            start, count = int(hunk.group(1)), int(hunk.group(2) or 1)
            added[path].update(range(start, start + count))
    return {name: frozenset(lines) for name, lines in added.items()}


def all_lines(source: str) -> frozenset[int]:
    return frozenset(range(1, len(source.splitlines()) + 1))


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


def _parse(path: str, source: str) -> ast.Module:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(source, filename=path)


def top_level_defs(path: str, source: str) -> tuple[Symbol, ...]:
    """Every top-level `def`, `async def` and `class`, private ones included."""
    kinds = {ast.FunctionDef: "function", ast.AsyncFunctionDef: "function", ast.ClassDef: "class"}
    return tuple(
        Symbol(path, node.name, kinds[type(node)], node.lineno)
        for node in _parse(path, source).body
        if type(node) in kinds
    )


def public_symbols(path: str, source: str) -> tuple[Symbol, ...]:
    return tuple(symbol for symbol in top_level_defs(path, source) if not symbol.name.startswith("_"))


def module_symbol(path: str) -> Symbol | None:
    stem = pathlib.PurePosixPath(path).stem
    return None if stem.startswith("_") else Symbol(path, stem, "module", 1)


def new_defs(changes: Iterable[Change], old: Mapping[str, str], new: Mapping[str, str]) -> tuple[Symbol, ...]:
    """Top-level defs (and new modules) in scope that the new sources have and the old ones do not."""
    found: list[Symbol] = []
    for change in changes:
        if change.status == "D" or not in_scope(change.new):
            continue
        before = old.get(change.old) if change.old and in_scope(change.old) else None
        if before is None:
            module = module_symbol(change.new)
            found.extend([module] if module else [])
        known = {symbol.name for symbol in top_level_defs(change.old or "", before)} if before is not None else set()
        found.extend(symbol for symbol in top_level_defs(change.new, new[change.new]) if symbol.name not in known)
    return tuple(found)


def added_symbols(changes: Iterable[Change], old: Mapping[str, str], new: Mapping[str, str]) -> tuple[Symbol, ...]:
    """The public symbols in scope that the diff adds: what must have a caller."""
    return tuple(symbol for symbol in new_defs(changes, old, new) if not symbol.name.startswith("_"))


def added_private(changes: Iterable[Change], old: Mapping[str, str], new: Mapping[str, str]) -> frozenset[tuple[str, str]]:
    """(path, name) of each private top-level def the diff adds: a use inside one counts only when it is called."""
    return frozenset((s.path, s.name) for s in new_defs(changes, old, new) if s.name.startswith("_"))


def _string_refs(value: str, line: int, owner: str | None) -> list[Ref]:
    if not DOTTED.fullmatch(value):
        return []
    return [Ref(part, line, owner) for part in re.split(r"[.:]", value)]


def _is_dunder_all(node: ast.AST) -> bool:
    targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
    return isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)) and any(
        isinstance(target, ast.Name) and target.id == "__all__" for target in targets
    )


def _node_refs(node: ast.AST, owner: str | None, reexport: bool) -> list[Ref]:
    if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Store):
        return [Ref(node.id, node.lineno, owner)]
    if isinstance(node, ast.Attribute):
        return [Ref(node.attr, node.end_lineno or node.lineno, owner)]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _string_refs(node.value, node.lineno, owner)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in NAME_LOOKUPS:
        name = node.args[1] if len(node.args) > 1 else None
        bare = isinstance(name, ast.Constant) and isinstance(name.value, str) and IDENTIFIER.fullmatch(name.value)
        return [Ref(name.value, name.lineno, owner)] if bare else []
    if isinstance(node, ast.Import) and not reexport:
        return [Ref(part, alias.lineno, owner) for alias in node.names for part in alias.name.split(".")]
    if isinstance(node, ast.ImportFrom) and not reexport:
        modules = [Ref(part, node.lineno, owner) for part in (node.module or "").split(".") if part]
        return modules + [Ref(alias.name, alias.lineno, owner) for alias in node.names]
    return []


def python_refs(path: str, source: str) -> tuple[Ref, ...]:
    """Every use of a name in a Python file, each with its line and the top-level def or class it sits in."""
    reexport = pathlib.PurePosixPath(path).name == "__init__.py"
    refs: list[Ref] = []
    for statement in _parse(path, source).body:
        if _is_dunder_all(statement):
            continue
        owner = statement.name if isinstance(statement, DEFS) else None
        pending: list[ast.AST] = [statement]
        while pending:
            node = pending.pop()
            pending.extend(ast.iter_child_nodes(node))
            refs.extend(_node_refs(node, owner, reexport))
    return tuple(refs)


def text_refs(source: str) -> tuple[Ref, ...]:
    """The parts of every dotted path in a config or script file: `pkg.mod:func` names pkg, mod and func."""
    return tuple(
        Ref(part, number, None)
        for number, line in enumerate(source.splitlines(), 1)
        for match in DOTTED.findall(line)
        for part in re.split(r"[.:]", match)
    )


def references(path: str, source: str) -> tuple[Ref, ...]:
    if path.endswith(".py"):
        try:
            return python_refs(path, source)
        except SyntaxError:
            return ()
    if pathlib.PurePosixPath(path).suffix in TEXT_SUFFIXES:
        return text_refs(source)
    return ()


def defined_names(path: str, source: str) -> frozenset[str]:
    try:
        names = {symbol.name for symbol in top_level_defs(path, source)}
    except SyntaxError:
        names = set()
    return frozenset(names | {pathlib.PurePosixPath(path).stem})


def parse_allowlist(text: str, sources: Mapping[str, str]) -> Allowlist:
    """`path::name  # reason` lines; the reason is required, and the path must exist and define the name."""
    entries: set[tuple[str, str]] = set()
    errors: list[str] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        entry, _, reason = line.partition("#")
        path, sep, name = entry.strip().partition("::")
        name = name.strip()
        if not sep or not path or not name:
            errors.append(f"allowlist line {number}: expected `path::name  # reason`, got {raw!r}")
        elif not reason.strip():
            errors.append(f"allowlist line {number}: {entry.strip()} has no reason; say why nothing calls it by name")
        elif path not in sources:
            errors.append(f"allowlist line {number}: {path} does not exist; drop the entry")
        elif name not in defined_names(path, sources[path]):
            errors.append(f"allowlist line {number}: {path} defines no {name}; drop the entry")
        else:
            entries.add((path, name))
    return Allowlist(frozenset(entries), tuple(errors))


def unwired(
    added: Sequence[Symbol],
    sources: Mapping[str, str],
    added_lines: Mapping[str, frozenset[int]],
    private: frozenset[tuple[str, str]],
    allow: frozenset[tuple[str, str]],
) -> tuple[Symbol, ...]:
    """The added symbols nothing calls: no counted use (see the module docstring) in any non-test source."""
    parsed: dict[str, tuple[Ref, ...]] = {}
    wired_memo: set[tuple[str, str]] = set()

    def refs_of(path: str) -> tuple[Ref, ...]:
        if path not in parsed:
            parsed[path] = references(path, sources[path])
        return parsed[path]

    def counts(target: tuple[str, str, bool], path: str, ref: Ref, visiting: frozenset[tuple[str, str]]) -> bool:
        target_path, name, is_module = target
        if path == target_path and (is_module or ref.owner == name):
            return False
        if path != target_path and ref.line not in added_lines.get(path, frozenset()):
            return False
        if ref.owner is not None and (path, ref.owner) in private:
            return wired((path, ref.owner, False), visiting)
        return True

    def wired(target: tuple[str, str, bool], visiting: frozenset[tuple[str, str]]) -> bool:
        key = target[:2]
        if key in wired_memo:
            return True
        if key in visiting:
            return False
        visiting = visiting | {key}
        for path, source in sources.items():
            if is_test_path(path) or target[1] not in source:
                continue
            if any(ref.name == target[1] and counts(target, path, ref, visiting) for ref in refs_of(path)):
                wired_memo.add(key)
                return True
        return False

    return tuple(
        symbol
        for symbol in added
        if (symbol.path, symbol.name) not in allow
        and not wired((symbol.path, symbol.name, symbol.kind == "module"), frozenset())
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


def main(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    parser = argparse.ArgumentParser(description="Fail when a diff adds a public symbol under src/ that nothing calls.")
    parser.add_argument("--base", default=default_base(environ))
    parser.add_argument("--root", default=".")
    parser.add_argument("--allow", default=DEFAULT_ALLOW)
    args = parser.parse_args(argv)
    try:
        root = pathlib.Path(_git(pathlib.Path(args.root), "rev-parse", "--show-toplevel").strip())
        base = _git(root, "merge-base", args.base, "HEAD").strip()
        changes = parse_name_status(_git(root, "diff", "--name-status", "-z", "-M", base, "--", "src"))
        diff = _git(
            root, "diff", "-U0", "-M", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/", base
        )
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
    readable = (path for path in paths if path.endswith(".py") or pathlib.PurePosixPath(path).suffix in TEXT_SUFFIXES)
    sources = {path: text for path in readable if not is_test_path(path) and (text := _read(root, path)) is not None}
    allow = parse_allowlist(_read(root, args.allow) or "", sources)
    if allow.errors:
        print("\n".join(allow.errors), file=sys.stderr)
        return 2
    new = {change.new: _read(root, change.new) or "" for change in changes if change.status != "D"}
    added_lines = {**parse_added_lines(diff), **{p: all_lines(sources[p]) for p in untracked if p in sources}}
    try:
        added = added_symbols(changes, old, new)
        private = added_private(changes, old, new)
    except SyntaxError as error:
        print(f"unwired: cannot parse {error.filename}: {error.msg}", file=sys.stderr)
        return 2
    found = unwired(added, sources, added_lines, private, allow.entries)
    print("\n".join(report(added, found)))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
