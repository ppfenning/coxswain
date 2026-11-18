"""Type guards for values read from JSON or YAML, and `conform`, which checks a decoded value against the type it is
read as.

The guards each narrow `object` to a container of `object`. `conform` is the validating constructor behind the
records read from the store (`landing_record.record_from_store`, `store_read.task_record_from_store`,
`chair_types.action_from_store`): it walks the declared type, TypedDicts included, and raises `ShapeError` naming the
first value that does not fit, so a malformed row fails where it is read rather than where it is used.
"""

from __future__ import annotations

import types
import typing
from collections.abc import Mapping
from enum import Enum
from typing import TypeGuard, cast

__all__ = ["ShapeError", "conform", "is_dict", "is_list", "is_mapping", "is_object_mapping"]


class ShapeError(ValueError):
    """A value read from JSON or the store that does not have the shape of the type it is read as."""


def is_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def is_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def is_mapping(value: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, Mapping)


def is_object_mapping(value: object) -> TypeGuard[Mapping[object, object]]:
    """A mapping whose keys are not known to be strings (YAML allows any scalar key)."""
    return isinstance(value, Mapping)


def conform[T](tp: type[T], raw: object, what: str) -> T:
    """Pure. `raw`, unchanged, as a `tp`; else `ShapeError` naming `what` and the first value that does not fit.

    A TypedDict's present keys are checked and its required keys must be there; keys it does not name pass, since a
    record may carry more than its reader names. An `int` fits `float`, a `bool` fits neither `int` nor `float`, and
    an Enum's plain value fits the Enum, as a kind read back from JSON is a plain string."""
    problem = _problem(cast(object, tp), raw, what)
    if problem is not None:
        raise ShapeError(problem)
    return cast(T, raw)


def _kind(value: object) -> str:
    return "null" if value is None else type(value).__name__


def _problem(tp: object, value: object, at: str) -> str | None:
    """The first way `value` misses `tp`, as one line naming the path `at`; None when it fits."""
    if tp is object or tp is typing.Any:
        return None
    if isinstance(tp, typing.TypeAliasType):
        return _problem(tp.__value__, value, at)
    if tp is None or tp is types.NoneType:
        return None if value is None else f"{at}: expected null, got {_kind(value)}"
    origin = typing.get_origin(tp)
    args: tuple[object, ...] = typing.get_args(tp)
    if origin is typing.Union or origin is types.UnionType:
        if any(_problem(arg, value, at) is None for arg in args):
            return None
        return f"{at}: {_kind(value)} {value!r:.60} fits none of {tp}"
    if origin is typing.Literal:
        return None if value in args else f"{at}: expected one of {list(args)}, got {value!r:.60}"
    if origin in (typing.Required, typing.NotRequired, typing.ReadOnly):
        return _problem(args[0], value, at)
    if origin is list:
        return _list_problem(args[0], value, at)
    if origin is dict:
        return _dict_problem(args[1], value, at)
    if typing.is_typeddict(tp):
        return _typeddict_problem(tp, value, at)
    if isinstance(tp, type):
        return _class_problem(tp, value, at)
    raise TypeError(f"conform cannot check {tp!r}")  # a type this reader does not know: a bug here, not bad input


def _list_problem(item: object, value: object, at: str) -> str | None:
    if not isinstance(value, list):
        return f"{at}: expected a list, got {_kind(value)}"
    items = cast(list[object], value)
    return next((p for i, element in enumerate(items) if (p := _problem(item, element, f"{at}[{i}]"))), None)


def _dict_problem(item: object, value: object, at: str) -> str | None:
    if not isinstance(value, dict):
        return f"{at}: expected an object, got {_kind(value)}"
    entries = cast(dict[object, object], value)
    bad_key = next((k for k in entries if not isinstance(k, str)), None)
    if bad_key is not None:
        return f"{at}: key {bad_key!r} is not a string"
    return next((p for k, v in entries.items() if (p := _problem(item, v, f"{at}.{k}"))), None)


def _typeddict_problem(tp: object, value: object, at: str) -> str | None:
    if not isinstance(value, dict):
        return f"{at}: expected an object, got {_kind(value)}"
    fields = cast(dict[str, object], value)
    hints: dict[str, object] = typing.get_type_hints(tp)
    required = cast(frozenset[str], getattr(tp, "__required_keys__", frozenset[str]()))
    missing = sorted(required - fields.keys())
    if missing:
        return f"{at}: missing {', '.join(missing)}"
    return next((p for k, hint in hints.items() if k in fields and (p := _problem(hint, fields[k], f"{at}.{k}"))), None)


def _class_problem(tp: type, value: object, at: str) -> str | None:
    if issubclass(tp, Enum):
        members: list[Enum] = list(tp)
        fits = isinstance(value, tp) or any(value == m.value for m in members)
        return None if fits else f"{at}: {value!r:.60} is not a {tp.__name__}"
    if tp is float:
        fits = isinstance(value, int | float) and not isinstance(value, bool)
    elif tp is int:
        fits = isinstance(value, int) and not isinstance(value, bool)
    else:
        fits = isinstance(value, tp)
    return None if fits else f"{at}: expected {tp.__name__}, got {_kind(value)}"
