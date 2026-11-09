"""Pure intake queue rules over `run_store.read_queue` rows: after, held, priority, order. No I/O, no clock."""

from collections.abc import Mapping
from typing import Any

Row = Mapping[str, Any]

TERMINAL = frozenset({"done", "dropped"})  # route.TERMINAL: the states that count an initiative's task as finished


def after_ids(extra: Mapping[str, Any]) -> list[str]:
    """`after` as a list of stripped, non-empty ids; one string is one id; [] when absent."""
    raw = extra.get("after")
    items = [raw] if isinstance(raw, str) else raw if isinstance(raw, (list, tuple)) else []
    stripped = [item.strip() for item in items if isinstance(item, str)]
    return [item for item in stripped if item]


def _unquoted(value: Any) -> str:
    """Lowercased text of a string, minus one pair of single or double quotes; "" for a non-string."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    inner = text[1:-1] if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"" else text
    return inner.strip().lower()


def _held_flag(extra: Mapping[str, Any]) -> bool:
    value = extra.get("held")
    return value is True or _unquoted(value) == "true"


def stage(extra: Mapping[str, Any]) -> str:
    """"draft" for `stage: draft` or `held: true`; "ready" otherwise, so an intake with no stage is queued for planning."""
    return "draft" if _held_flag(extra) or _unquoted(extra.get("stage")) == "draft" else "ready"


def is_held(extra: Mapping[str, Any]) -> bool:
    """True when the intake is a draft: `stage: draft`, or `held: true` as a bool or a quoted string, any case."""
    return stage(extra) == "draft"


def intake_priority(extra: Mapping[str, Any]) -> int:
    """0 when `priority` is absent or not an int; a bool is not a number here."""
    value = extra.get("priority")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _landed_initiatives(rows: list[Row]) -> set[str]:
    """route.initiative_states: at least one task row, and every one `done` or `dropped`."""
    states: dict[str, list[str]] = {}
    for row in rows:
        if row["kind"] == "task":
            states.setdefault(row["initiative"], []).append(row["state"])
    return {name for name, seen in states.items() if all(state in TERMINAL for state in seen)}


def _intake_ids(row: Row) -> set[str]:
    """Both ids an intake answers to: its file name and its `id:` field."""
    own = row["extra"].get("id")
    return {row["task_id"], *([str(own)] if own else [])}


def landed_ids(rows: list[Row]) -> set[str]:
    """Landed initiative ids, plus both ids of every non-removed intake whose `extra.initiative` has landed."""
    initiatives = _landed_initiatives(rows)
    intakes = [row for row in rows if row["kind"] == "intake" and row["state"] != "removed"]
    named = [row for row in intakes if row["extra"].get("initiative") in initiatives]
    return initiatives.union(*(_intake_ids(row) for row in named))


def unmet_after(row: Row, rows: list[Row]) -> list[str]:
    """Ids in the row's `after` not yet landed, in declared order; an id naming nothing is unmet."""
    landed = landed_ids(rows)
    return [name for name in after_ids(row["extra"]) if name not in landed]


def _queued_intake(rows: list[Row]) -> list[Row]:
    return [row for row in rows if row["kind"] == "intake" and row["state"] == "queued"]


def runnable_intake(rows: list[Row]) -> list[Row]:
    """Queued, not held, no unmet `after`; priority descending, ties in input order. A new list."""
    landed = landed_ids(rows)
    ready = [
        row
        for row in _queued_intake(rows)
        if not is_held(row["extra"]) and all(name in landed for name in after_ids(row["extra"]))
    ]
    return sorted(ready, key=lambda row: -intake_priority(row["extra"]))


def waiting_on(rows: list[Row]) -> dict[str, list[str]]:
    """Intake task_id -> unmet ids, for queued intake that is not held and still waits."""
    landed = landed_ids(rows)
    unmet = {
        row["task_id"]: [name for name in after_ids(row["extra"]) if name not in landed]
        for row in _queued_intake(rows)
        if not is_held(row["extra"])
    }
    return {task_id: names for task_id, names in unmet.items() if names}


def held_rows(rows: list[Row]) -> list[Row]:
    return [row for row in _queued_intake(rows) if is_held(row["extra"])]
