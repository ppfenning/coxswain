"""Which insta snapshots count as inside a task's surfaces. Pure string rules, no file reads.

insta names a snapshot `<crate>__<module path>__tests__<test name>.snap` in a `snapshots/` directory beside the
module's sources. `.snap.new` marks the pending copy of a snapshot that is not accepted yet.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

_SNAPSHOTS_DIR = "snapshots"
_PENDING = ".snap.new"


def snapshot_module_paths(path: str) -> list[str]:
    """Source files a `.snap` path names; empty when it does not parse as a module snapshot.

    The module path is the `__` segments between the crate and `tests`. Its last segment is the module: the
    directory's own module is `mod.rs`, any other is `<segment>.rs` beside `snapshots/`.
    unknown: a nested module laid out as `<segment>/mod.rs` is not named.
    """
    parts = path.split("/")
    if not path.endswith(".snap") or len(parts) < 2 or parts[-2] != _SNAPSHOTS_DIR:
        return []
    names = parts[-1][: -len(".snap")].split("__")
    if "tests" not in names[1:]:
        return []
    modules = names[1 : names.index("tests", 1)]
    if not modules or not all(modules):
        return []
    beside = parts[:-2]
    module = modules[-1]
    own = bool(beside) and beside[-1] == module
    return ["/".join([*beside, "mod.rs" if own else f"{module}.rs"])]


def drop_pending_snapshots(touched: Sequence[str]) -> list[str]:
    """`touched` without `.snap.new` paths; the accepted `.snap` is the one that counts."""
    return [path for path in touched if not path.endswith(_PENDING)]


def is_surface_snapshot(path: str, declared: Collection[str]) -> bool:
    """Whether `path` is a module snapshot whose module is one of `declared`, the surfaces with `(new)` stripped."""
    return any(source in declared for source in snapshot_module_paths(path))
