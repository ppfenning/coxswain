"""The provider profile: tiers resolved to exact catalog ids.

    load_profile(path, catalog_path) -> Profile | list[str]   the only file reads
    resolve_profile(raw, catalog)    -> Profile | list[str]   raw is a plain dict
    resolve_entry(name, catalog)     -> ResolvedModel | str   one tier entry

A family alias (sonnet, opus-latest) resolves to the newest exact id in its
family, even when the catalog binds that alias to an older row. A catalog id
resolves to itself; a pinned catalog alias (claude-haiku-4-5) to its row's id.
Any other name is an error. The resolved `model` is therefore always an exact
catalog id; `requested` keeps the name as written. Failures are returned as
messages, never raised.

Nothing outside this repo calls `load_profile` yet. The runner that reads
providers/*.yaml must call it before any shipped tier names a family alias,
or run records will receive the bare alias.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from core.catalog import (
    Catalog,
    entry_by_alias,
    is_family_alias,
    load_catalog,
    newest_in_family,
)

__all__ = [
    "MergeQueue",
    "Profile",
    "ResolvedModel",
    "load_profile",
    "parse_max_parallel",
    "parse_merge_queue",
    "resolve_entry",
    "resolve_profile",
]


@dataclass(frozen=True)
class ResolvedModel:
    model: str
    requested: str


@dataclass(frozen=True)
class MergeQueue:
    batch_size: int = 1
    wait_s: float = 0.0


@dataclass(frozen=True)
class Profile:
    name: str
    tiers: Mapping[str, ResolvedModel]
    raw: Mapping[str, Any]
    max_parallel: int | None = None
    merge_queue: MergeQueue | None = None


def parse_max_parallel(value: Any) -> int | str | None:
    """None when absent, the int when positive, else a message. A bool is refused: it is an int subclass."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return f"max_parallel must be a positive integer, got {value!r}"


_MERGE_QUEUE_KEYS = frozenset({"batch_size", "wait_s"})


def _is_number(value: Any) -> bool:
    """A bool is refused: it is an int subclass."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse_merge_queue(value: Any) -> MergeQueue | str | None:
    """None when absent, a MergeQueue when valid, else one message listing every problem."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        return f"merge_queue must be a mapping, got {value!r}"
    batch_size = value.get("batch_size", 1)
    wait_s = value.get("wait_s", 0.0)
    unknown = sorted(str(k) for k in value if k not in _MERGE_QUEUE_KEYS)
    problems = [
        *([f"merge_queue has unknown key(s) {', '.join(repr(k) for k in unknown)}"] if unknown else []),
        *(
            []
            if isinstance(batch_size, int) and not isinstance(batch_size, bool) and batch_size >= 1
            else [f"merge_queue.batch_size must be an integer of at least 1, got {batch_size!r}"]
        ),
        *(
            []
            if _is_number(wait_s) and wait_s >= 0
            else [f"merge_queue.wait_s must be a number of at least 0, got {wait_s!r}"]
        ),
    ]
    return "; ".join(problems) if problems else MergeQueue(batch_size=batch_size, wait_s=float(wait_s))


def _resolve_alias(name: str, catalog: Catalog) -> ResolvedModel | str:
    family = name.removesuffix("-latest")
    chosen = newest_in_family((e.id for e in catalog.entries), family)
    return (
        ResolvedModel(model=chosen, requested=name)
        if chosen is not None
        else f"alias {name!r}: no catalog row for family {family!r}"
    )


def _resolve_named(name: str, catalog: Catalog) -> ResolvedModel:
    """A catalog alias maps to its row's id; any other name (an exact id, a local model) passes through unchanged."""
    entry = entry_by_alias(catalog, name)
    return ResolvedModel(model=entry.id if entry is not None else name, requested=name)


def resolve_entry(name: str, catalog: Catalog) -> ResolvedModel | str:
    """A family alias goes to the newest row in its family; anything else resolves as `_resolve_named` says."""
    return _resolve_alias(name, catalog) if is_family_alias(name) else _resolve_named(name, catalog)


def resolve_profile(raw: Any, catalog: Catalog) -> Profile | list[str]:
    """Resolve every tier; report every failure at once."""
    tiers = raw.get("tiers") if isinstance(raw, dict) else None
    if not isinstance(tiers, dict) or not tiers:
        return ["profile must have a non-empty 'tiers' mapping"]
    resolved = {tier: resolve_entry(str(name), catalog) for tier, name in tiers.items()}
    cap = parse_max_parallel(raw.get("max_parallel"))
    queue = parse_merge_queue(raw.get("merge_queue"))
    errors = [
        *(f"tier {t!r}: {r}" for t, r in resolved.items() if isinstance(r, str)),
        *([cap] if isinstance(cap, str) else []),
        *([queue] if isinstance(queue, str) else []),
    ]
    return (
        errors
        if errors
        else Profile(
            name=str(raw.get("profile", "")),
            tiers=MappingProxyType({t: r for t, r in resolved.items() if isinstance(r, ResolvedModel)}),
            raw=MappingProxyType(dict(raw)),
            max_parallel=cap if isinstance(cap, int) else None,
            merge_queue=queue if isinstance(queue, MergeQueue) else None,
        )
    )


def load_profile(path: Path, catalog_path: Path) -> Profile | list[str]:
    """The edge: read the profile and the catalog, hand both to `resolve_profile`."""
    catalog = load_catalog(catalog_path)
    if isinstance(catalog, list):
        return catalog
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return [f"{path}: cannot read: {exc}"]
    except yaml.YAMLError as exc:
        return [f"{path}: invalid yaml: {exc}"]
    return resolve_profile(raw, catalog)
