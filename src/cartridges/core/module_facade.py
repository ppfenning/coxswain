"""A module split into parts that still answers for every name it used to define (H9, docs/hardening.md).

A module that grew past what one file should hold moves its functions into part modules, unchanged. Callers and tests
keep reading and patching the old module: `install` makes it a facade, so

- an attribute the facade lacks is read from the first part that defines it (`attribute`, called from the facade's
  own module-level `__getattr__`), importing the parts on that first read when the facade loads them lazily;
- a write or delete on the facade, a test's `monkeypatch.setattr(module, name, fake)` and its undo, reaches every part
  that holds the name, so a function moved into a part sees the patch exactly as it did when both lived in one file.

Undoing a write restores each part's own value: the facade remembers, the first time a name is written, what it and
each part held, and a later write of the facade's original value puts every part's original back.
"""

from __future__ import annotations

import importlib
import sys
import types
from collections.abc import Sequence

PARTS_KEY = "__facade_parts__"
SEEN_KEY = "__facade_seen__"
_MISSING = object()
_import = importlib.import_module  # bound once: a test that patches importlib.import_module must not reach the parts

type Snapshot = tuple[object, tuple[tuple[types.ModuleType, object], ...]]


def parts(module: types.ModuleType) -> tuple[types.ModuleType, ...]:
    """The facade's part modules, in order, each imported on first use."""
    names: tuple[str, ...] = module.__dict__.get(PARTS_KEY, ())
    return tuple(sys.modules.get(name) or _import(name) for name in names)


def _seen(module: types.ModuleType) -> dict[str, Snapshot]:
    if SEEN_KEY not in module.__dict__:
        module.__dict__[SEEN_KEY] = {}
    seen: dict[str, Snapshot] = module.__dict__[SEEN_KEY]
    return seen


class FacadeModule(types.ModuleType):
    """A module whose writes and deletes also reach the parts that hold the same name."""

    def __setattr__(self, name: str, value: object) -> None:
        held = parts(self)  # first: a part's own import may register into a facade global this write replaces
        seen = _seen(self)
        if name not in seen:
            seen[name] = (
                self.__dict__.get(name, _MISSING),
                tuple((part, part.__dict__[name]) for part in held if name in part.__dict__),
            )
        original, originals = seen[name]
        super().__setattr__(name, value)
        for part, part_value in originals:
            setattr(part, name, part_value if value is original else value)

    def __delattr__(self, name: str) -> None:
        super().__delattr__(name)
        snapshot = _seen(self).pop(name, None)
        if snapshot is not None:
            for part, part_value in snapshot[1]:
                setattr(part, name, part_value)


def install(module_name: str, part_names: Sequence[str]) -> None:
    """Make the module `module_name` (already in `sys.modules`) a facade over `part_names`, which are not imported here."""
    module = sys.modules[module_name]
    module.__dict__[PARTS_KEY] = tuple(part_names)
    module.__class__ = FacadeModule


def attribute(module_name: str, name: str) -> object:
    """`name` read from the first part of the facade `module_name` that defines it, kept on the facade for the next read.

    A dunder is never looked up in the parts: import machinery and introspection probe those, and must not load them."""
    module = sys.modules[module_name]
    if not (name.startswith("__") and name.endswith("__")):
        for part in parts(module):
            if name in part.__dict__:
                value = part.__dict__[name]
                module.__dict__[name] = value
                return value
    raise AttributeError(f"module {module_name!r} has no attribute {name!r}")
