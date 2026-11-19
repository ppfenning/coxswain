"""The `chair_actions` rows one chair process keeps across ticks, and the views of a row each tick's readers share (H6).

A quiet tick reads about 180,000 of these rows at the chair machine's volume, and some twenty readers each turned every
row into a doc of their own (`action_json` laid over the columns, or the columns over it): 3.2 million merges a tick,
most of a 13 s tick. A `FrozenRow` keeps each view of itself the first time a reader asks for it (`row_view`), so a row
is merged once in the process's life, however many readers and ticks read it.

That is safe because a row never changes after the read: `chair_actions` is insert-only, and a `FrozenRow` refuses every
edit, so a reader that tried to change a shared row fails loudly instead of changing what every later reader sees. The
freeze goes all the way down: a mapping inside a row (`action_json`) is a `FrozenRow` too, and a list (`intake_ids`,
`stale_tasks`) a `FrozenList`, which is still a `list` to every `isinstance` check. A view is frozen the same way.
A deep copy (`copy.deepcopy`, a pickle) is plain all the way down. A shallow one (`dict(row)`, `{**row}`) is a plain dict
whose nested values are still the frozen ones, so an edit of a nested value through it fails as well.

A row that is not a `FrozenRow` (a test's literal dict, a SQLite file read) keeps nothing: `row_view` computes the view
each time, exactly as the reader did before.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any, NoReturn, cast

__all__ = ["FrozenList", "FrozenRow", "freeze", "row_view"]


_MISSING: Any = object()


def _refuse(*_args: object, **_kwargs: object) -> NoReturn:
    raise TypeError("a chair_actions row is shared by every reader of the tick and is never edited; copy it first")


def _frozen(value: object) -> object:
    """Pure. `value` with every dict in it a `FrozenRow` and every list a `FrozenList`; frozen values come back as they are."""
    if isinstance(value, (FrozenRow, FrozenList)):
        return value
    if isinstance(value, dict):
        return FrozenRow(cast(Mapping[str, object], value))
    if isinstance(value, list):
        return FrozenList(cast(list[object], value))
    return value


class FrozenList(list[object]):
    """A list inside a kept row: no one can edit it, and its items are frozen as well."""

    __slots__ = ()

    def __init__(self, items: Iterable[object] = ()) -> None:
        super().__init__(_frozen(item) for item in items)

    def __reduce__(self) -> tuple[type[list[object]], tuple[list[object]]]:
        """A pickle is a plain list."""
        return (list, (list(self),))

    def __deepcopy__(self, memo: dict[int, object]) -> list[object]:
        import copy  # the module never needs it otherwise

        return [copy.deepcopy(v, memo) for v in self]

    def __copy__(self) -> list[object]:
        return list(self)

    __setitem__ = _refuse
    __delitem__ = _refuse
    __iadd__ = _refuse
    __imul__ = _refuse
    append = _refuse
    extend = _refuse
    insert = _refuse
    remove = _refuse
    pop = _refuse
    clear = _refuse
    sort = _refuse
    reverse = _refuse


class FrozenRow(dict[str, object]):
    """A row no one can edit, nested values included, with room for its views (`row_view`)."""

    __slots__ = ("views",)

    views: dict[Callable[..., object], object]

    def __init__(self, items: Mapping[str, object]) -> None:
        super().__init__({k: _frozen(v) for k, v in items.items()})
        self.views = {}

    def __reduce__(self) -> tuple[type[dict[str, object]], tuple[dict[str, object]]]:
        """A pickle is a plain dict: the caller's own, to edit."""
        return (dict, (dict(self),))

    def __deepcopy__(self, memo: dict[int, object]) -> dict[str, object]:
        import copy  # the module never needs it otherwise

        return {k: copy.deepcopy(v, memo) for k, v in self.items()}

    def __copy__(self) -> dict[str, object]:
        return dict(self)

    __setitem__ = _refuse
    __delitem__ = _refuse
    __ior__ = _refuse
    clear = _refuse
    pop = _refuse
    popitem = _refuse
    setdefault = _refuse
    update = _refuse


def freeze(row: Mapping[str, object]) -> FrozenRow:
    """Pure. `row` frozen all the way down, as a `FrozenRow` with no views yet; a `FrozenRow` comes back as it is."""
    return row if isinstance(row, FrozenRow) else FrozenRow(row)


def row_view[T](row: Mapping[str, object], view: Callable[[Mapping[str, object]], T]) -> T:
    """`view(row)`, kept on a `FrozenRow` the first time so every later call for the same `view` returns it.

    `view` must be a pure function of the row and a module-level function, so one function object names one view. A
    mapping view is kept frozen, so no reader can edit what the next one is handed. Called millions of times a tick,
    so the kept path is one attribute read and one lookup."""
    views: dict[Callable[..., object], Any] | None = getattr(row, "views", None)
    if views is None:
        return view(row)
    found: Any = views.get(view, _MISSING)
    if found is _MISSING:
        value = view(row)
        found = views[view] = freeze(cast(Mapping[str, object], value)) if isinstance(value, dict) else value
    return found
