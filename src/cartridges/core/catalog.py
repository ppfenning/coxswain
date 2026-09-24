"""The model catalog: which models exist, what they cost, what they are good for.

    load_catalog(path)          -> Catalog | list[str]    the only file read
    parse_catalog(raw)          -> Catalog | list[str]    raw is a plain dict
    entries_for_class(c, cls)   -> tuple[ModelEntry, ...] cheapest first
    entry_by_id / entry_by_alias -> ModelEntry | None

Failures are returned as a list of messages, never raised, so a caller can show
every problem in a catalog at once instead of the first one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "CLASSES",
    "Catalog",
    "ModelEntry",
    "Price",
    "entries_for_class",
    "entry_by_alias",
    "entry_by_id",
    "load_catalog",
    "parse_catalog",
]

CLASSES = frozenset({"extract", "reason", "judge", "frontier"})
_PRICE_KEYS = ("input", "output", "cache_write", "cache_read")


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: float
    output: float
    cache_write: float
    cache_read: float


@dataclass(frozen=True)
class ModelEntry:
    id: str
    aliases: tuple[str, ...]
    provider: str
    price: Price
    effort: tuple[str, ...]
    classes: tuple[str, ...]
    context_window: int | None  # None means unknown, not unlimited


@dataclass(frozen=True)
class Catalog:
    entries: tuple[ModelEntry, ...]


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _str_list(v: Any) -> list[str] | None:
    return list(v) if isinstance(v, list) and all(isinstance(s, str) for s in v) else None


def _price_errors(label: str, price: Any) -> list[str]:
    if not isinstance(price, dict):
        return [f"{label}: price must be a mapping"]
    return [
        f"{label}: price.{k} must be a positive number"
        for k in _PRICE_KEYS
        if not (_is_number(price.get(k)) and price[k] > 0)
    ]


def _entry_errors(i: int, e: Any) -> list[str]:
    if not isinstance(e, dict):
        return [f"models[{i}]: must be a mapping"]
    label = f"models[{i}] ({e.get('id')})"
    aliases = _str_list(e.get("aliases", []))
    effort = _str_list(e.get("effort"))
    classes = _str_list(e.get("classes"))
    window = e.get("context_window")
    return [
        *([] if isinstance(e.get("id"), str) and e["id"] else [f"{label}: id must be a non-empty string"]),
        *([] if aliases is not None else [f"{label}: aliases must be a list of strings"]),
        *([] if isinstance(e.get("provider"), str) and e["provider"] else [f"{label}: provider must be a non-empty string"]),
        *_price_errors(label, e.get("price")),
        *([] if effort is None or effort else [f"{label}: effort list is empty"]),
        *([] if effort is not None else [f"{label}: effort must be a list of strings"]),
        *([] if classes is not None else [f"{label}: classes must be a list of strings"]),
        *[
            f"{label}: unknown capability class {c!r}"
            for c in (classes or [])
            if c not in CLASSES
        ],
        *(
            []
            if window is None or (isinstance(window, int) and not isinstance(window, bool) and window > 0)
            else [f"{label}: context_window must be a positive integer or null"]
        ),
    ]


def _duplicates(names: list[str]) -> list[str]:
    return sorted({n for n in names if names.count(n) > 1})


def _collision_errors(entries: list[dict[str, Any]]) -> list[str]:
    ids = [e["id"] for e in entries if isinstance(e.get("id"), str)]
    aliases = [
        a
        for e in entries
        for a in (_str_list(e.get("aliases", [])) or [])
    ]
    return [
        *[f"duplicate id {n!r}" for n in _duplicates(ids)],
        *[f"duplicate alias {n!r}" for n in _duplicates(aliases)],
        *[f"alias {n!r} collides with a model id" for n in sorted(set(aliases) & set(ids))],
    ]


def _build(e: dict[str, Any]) -> ModelEntry:
    return ModelEntry(
        id=e["id"],
        aliases=tuple(e.get("aliases", [])),
        provider=e["provider"],
        price=Price(**{k: float(e["price"][k]) for k in _PRICE_KEYS}),
        effort=tuple(e["effort"]),
        classes=tuple(e["classes"]),
        context_window=e.get("context_window"),
    )


def parse_catalog(raw: Any) -> Catalog | list[str]:
    """Validate a plain dict and build a Catalog, or return every error found."""
    models = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(models, list) or not models:
        return ["catalog must have a non-empty 'models' list"]
    errors = [
        *[m for i, e in enumerate(models) for m in _entry_errors(i, e)],
        *_collision_errors([e for e in models if isinstance(e, dict)]),
    ]
    return errors if errors else Catalog(entries=tuple(_build(e) for e in models))


def entries_for_class(catalog: Catalog, cls: str) -> tuple[ModelEntry, ...]:
    """Models carrying `cls`, cheapest first by input then output price."""
    return tuple(
        sorted(
            (e for e in catalog.entries if cls in e.classes),
            key=lambda e: (e.price.input, e.price.output, e.id),
        )
    )


def entry_by_id(catalog: Catalog, model_id: str) -> ModelEntry | None:
    return next((e for e in catalog.entries if e.id == model_id), None)


def entry_by_alias(catalog: Catalog, alias: str) -> ModelEntry | None:
    return next((e for e in catalog.entries if alias in e.aliases), None)


def load_catalog(path: Path) -> Catalog | list[str]:
    """The edge: read the file, hand the parsed dict to `parse_catalog`."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return [f"{path}: cannot read: {exc}"]
    except yaml.YAMLError as exc:
        return [f"{path}: invalid yaml: {exc}"]
    return parse_catalog(raw)
