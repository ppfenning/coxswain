"""Pure release gate over `run_store.read_queue` rows: version compare, held and deferred initiatives. No I/O, no clock."""

import re
from collections.abc import Sequence
from typing import Any

from agent_tools.intake_queue import Row, is_held

_LEADING_VERSION = re.compile(r"^v?(\d+(?:\.\d+)*)")


def _text(value: Any) -> str:
    """A scalar's stripped string form; '' for None."""
    return "" if value is None else str(value).strip()


def _extra(row: Row) -> Row:
    """The row's `extra` mapping, empty when absent."""
    return row.get("extra") or {}


def _named_intakes(rows: Sequence[Row]) -> list[Row]:
    """Non-removed intake rows whose `extra.initiative` is set."""
    return [r for r in rows if r["kind"] == "intake" and r["state"] != "removed" and _text(_extra(r).get("initiative"))]


def release_tuple(text: str) -> tuple[int, ...] | None:
    """The leading dotted numeric part of a version, a leading `v` dropped; None when there is none."""
    found = _LEADING_VERSION.match(_text(text))
    return tuple(int(part) for part in found.group(1).split(".")) if found else None


def is_deferred(target: str, current: str) -> bool:
    """True only when both versions parse and current is strictly below target."""
    wanted, have = release_tuple(target), release_tuple(current)
    return wanted is not None and have is not None and have < wanted


def held_initiatives(rows: Sequence[Row]) -> frozenset[str]:
    """Initiative ids held on their own row or by a non-removed held intake that names them."""
    own = {str(r.get("initiative") or r["task_id"]) for r in rows if r["kind"] == "initiative" and is_held(_extra(r))}
    named = {_text(_extra(r)["initiative"]) for r in _named_intakes(rows) if is_held(_extra(r))}
    return frozenset(own | named)


def deferred_initiatives(rows: Sequence[Row], current: str) -> dict[str, str]:
    """Initiative id to target release, from its intake or its own row (which wins), when current is below it."""
    from_intakes = {
        _text(_extra(r)["initiative"]): _text(_extra(r).get("target_release")) for r in _named_intakes(rows)
    }
    from_rows = {
        str(r.get("initiative") or r["task_id"]): _text(_extra(r).get("target_release"))
        for r in rows
        if r["kind"] == "initiative"
    }
    merged = {**{k: v for k, v in from_intakes.items() if v}, **{k: v for k, v in from_rows.items() if v}}
    return {name: target for name, target in merged.items() if is_deferred(target, current)}


def deferred_intakes(rows: Sequence[Row], current: str) -> dict[str, str]:
    """Queued intake task_id to target release for intakes whose target is above current."""
    targets = {
        str(r["task_id"]): _text(_extra(r).get("target_release"))
        for r in rows
        if r["kind"] == "intake" and r["state"] == "queued"
    }
    return {task_id: target for task_id, target in targets.items() if target and is_deferred(target, current)}
