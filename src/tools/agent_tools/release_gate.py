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


def working_release(have: tuple[int, ...]) -> tuple[int, ...]:
    """The release in progress on top of `have`: its last part plus one (2026.11.5 -> 2026.11.6)."""
    return (*have[:-1], have[-1] + 1)


def is_deferred(target: str, current: str) -> bool:
    """True only when both versions parse and target is beyond the release in progress.

    `target_release` names the release a piece of work ships in, so work for the next release (the current
    version's last part plus one) is open now, and only work for a later release waits for the next cut."""
    wanted, have = release_tuple(target), release_tuple(current)
    return wanted is not None and have is not None and working_release(have) < wanted


def held_initiatives(rows: Sequence[Row]) -> frozenset[str]:
    """Initiative ids held on their own row or by a non-removed held intake that names them."""
    own = {str(r.get("initiative") or r["task_id"]) for r in rows if r["kind"] == "initiative" and is_held(_extra(r))}
    named = {_text(_extra(r)["initiative"]) for r in _named_intakes(rows) if is_held(_extra(r))}
    return frozenset(own | named)


def initiative_values(rows: Sequence[Row], key: str) -> dict[str, str]:
    """Initiative id to the non-empty `extra[key]` of its intake or its own row, which wins. An intake names its
    initiative by `extra.initiative`, and an initiative its intake by `extra.intake` (`intake/<id>.md`); either link
    counts."""
    intakes = [r for r in rows if r["kind"] == "intake" and r["state"] != "removed"]
    by_path = {f"intake/{r['task_id']}.md": _text(_extra(r).get(key)) for r in intakes}
    own = [r for r in rows if r["kind"] == "initiative"]
    linked = {str(r.get("initiative") or r["task_id"]): by_path.get(_text(_extra(r).get("intake")), "") for r in own}
    named = {_text(_extra(r)["initiative"]): _text(_extra(r).get(key)) for r in _named_intakes(rows)}
    from_rows = {str(r.get("initiative") or r["task_id"]): _text(_extra(r).get(key)) for r in own}
    return {k: v for layer in (linked, named, from_rows) for k, v in layer.items() if v}


def deferred_initiatives(rows: Sequence[Row], current: str) -> dict[str, str]:
    """Initiative id to target release, from its intake or its own row (which wins), when current is below it."""
    merged = initiative_values(rows, "target_release")
    return {name: target for name, target in merged.items() if is_deferred(target, current)}


def deferred_intakes(rows: Sequence[Row], current: str) -> dict[str, str]:
    """Queued intake task_id to target release for intakes whose target is above current."""
    targets = {
        str(r["task_id"]): _text(_extra(r).get("target_release"))
        for r in rows
        if r["kind"] == "intake" and r["state"] == "queued"
    }
    return {task_id: target for task_id, target in targets.items() if target and is_deferred(target, current)}
