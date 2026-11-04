"""The work-store DAG problem of each initiative, from the ticket rows the chair already reads.

The check is `core.workstore.validate_dag`, the one the epic driver runs through `read_initiative`."""

from collections.abc import Mapping, Sequence
from typing import Any

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


def dag_problems(rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Initiative id to its first work-store DAG problem; a valid initiative has no entry.

    A need on a task of another initiative is not dangling. A cycle reads `cycle: a -> b -> a`."""
    initiatives = list(dict.fromkeys(str(r["initiative"]) for r in rows))

    def problem(initiative: str) -> str | None:
        items = [
            {"id": r["id"], "needs": list(r.get("needs") or []), "state": r.get("state")}
            for r in rows
            if str(r["initiative"]) == initiative
        ]
        foreign = {str(r["id"]): str(r.get("state")) for r in rows if str(r["initiative"]) != initiative}
        try:
            validate_dag(items, foreign)
        except WorkStoreError as err:
            return _first_problem(items, foreign, str(err))
        return None

    found = {i: problem(i) for i in initiatives}
    return {i: p for i, p in found.items() if p is not None}
