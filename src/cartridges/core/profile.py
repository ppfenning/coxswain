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

__all__ = ["Profile", "ResolvedModel", "load_profile", "resolve_entry", "resolve_profile"]


@dataclass(frozen=True)
class ResolvedModel:
    model: str
    requested: str


@dataclass(frozen=True)
class Profile:
    name: str
    tiers: Mapping[str, ResolvedModel]
    raw: Mapping[str, Any]


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
    errors = [f"tier {t!r}: {r}" for t, r in resolved.items() if isinstance(r, str)]
    return (
        errors
        if errors
        else Profile(
            name=str(raw.get("profile", "")),
            tiers=MappingProxyType({t: r for t, r in resolved.items() if isinstance(r, ResolvedModel)}),
            raw=MappingProxyType(dict(raw)),
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
