"""Type guards for values read from JSON or YAML: each narrows `object` to a container of `object`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeGuard

__all__ = ["is_dict", "is_list", "is_mapping", "is_object_mapping"]


def is_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def is_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def is_mapping(value: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, Mapping)


def is_object_mapping(value: object) -> TypeGuard[Mapping[object, object]]:
    """A mapping whose keys are not known to be strings (YAML allows any scalar key)."""
    return isinstance(value, Mapping)
