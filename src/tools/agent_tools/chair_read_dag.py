"""The work-store DAG problem of each initiative, from the ticket rows the chair already reads.

The check is `core.workstore.validate_dag`, the one the epic driver runs through `read_initiative`."""

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from core.landing_record import phase_cycle, phase_parents
from core.workstore import DONE, DROPPED, WorkStoreError, validate_dag

_CYCLE = "dependency cycle: "


def _first_problem(items: Sequence[Mapping[str, Any]], foreign: Mapping[str, str], message: str) -> str:
    """The first problem in row order: a missing need, then a dropped one, then the cycle, then the first line of `message`."""
    ids = {str(i["id"]) for i in items}
    dropped = {str(i["id"]) for i in items if i.get("state") == DROPPED}
    live = [i for i in items if i.get("state") != DROPPED]
    missing = [f"{i['id']} needs {n}" for i in live for n in i["needs"] if n not in ids and n not in foreign]
    gone = [f"{i['id']} needs {n} (dropped)" for i in live for n in i["needs"] if n in dropped and i.get("state") != DONE]
    lines = [line.removeprefix("  - ") for line in message.splitlines()[1:]]
    cycles = [f"cycle: {line.removeprefix(_CYCLE)}" for line in lines if line.startswith(_CYCLE)]
    return next(iter([*missing, *gone, *cycles, *lines, message]))


class _Foreign(Mapping[str, str]):
    """Task id to state over the rows of every initiative but `own`, the last such row winning, as the dict it replaces
    read; a view over rows grouped once, so asking it about one initiative does not walk the whole board."""

    def __init__(self, owners: Mapping[str, Sequence[tuple[str, str]]], own: str, any_other: bool) -> None:
        self._owners, self._own, self._any_other = owners, own, any_other

    def __getitem__(self, key: str) -> str:
        states = [state for initiative, state in self._owners.get(key, ()) if initiative != self._own]
        if not states:
            raise KeyError(key)
        return states[-1]

    def __iter__(self) -> Iterator[str]:
        return (key for key, by in self._owners.items() if any(initiative != self._own for initiative, _ in by))

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __bool__(self) -> bool:
        return self._any_other


def dag_problems(rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Initiative id to its first work-store DAG problem; a valid initiative has no entry.

    A need on a task of another initiative is not dangling. A cycle reads `cycle: a -> b -> a`."""
    initiatives = list(dict.fromkeys(str(r["initiative"]) for r in rows))
    # Grouped once: building each initiative's items and foreign map from every row made this quadratic in the board.
    own: dict[str, list[dict[str, Any]]] = {i: [] for i in initiatives}
    owners: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        own[str(r["initiative"])].append(
            {"id": r["id"], "needs": list(r.get("needs") or []), "state": r.get("state"), "phase": r.get("phase")}
        )
        owners.setdefault(str(r["id"]), []).append((str(r["initiative"]), str(r.get("state"))))

    def problem(initiative: str) -> str | None:
        items = own[initiative]
        foreign = _Foreign(owners, initiative, len(initiatives) > 1)
        try:
            validate_dag(items, foreign)
        except WorkStoreError as err:
            return _first_problem(items, foreign, str(err))
        # Tasks with no cycle can still make one between phases (a task of p1 needs p2, one of p2 needs p1): neither
        # phase can merge first, so the initiative is reported, not left waiting in silence.
        cycle = phase_cycle(phase_parents(items))
        return f"phase cycle: {' -> '.join(cycle)}" if cycle else None

    found = {i: problem(i) for i in initiatives}
    return {i: p for i, p in found.items() if p is not None}
