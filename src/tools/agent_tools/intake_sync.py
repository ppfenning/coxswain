"""One-way sync of a source's tickets into the store's intake rows. Pure: the plan only; the edge is in `cli`.

The source is where a ticket is written; the store is where the work is. So text flows in and never back:

- While an intake is still queued (draft, ready or held, not yet decomposed), an edit at the source replaces the
  stored title and body. Nothing has been built from the old text, so the newest text is the ticket.
- Once it is decomposed, the stored initiative is the contract the tasks were cut from. An edit at the source is
  recorded on the initiative as `changed_upstream` and changes no text; what happens next is the orchestrator's rule.
- An intake whose work has landed or is done is left alone.

Each row remembers the digest of the source text it last saw, so one edit is acted on once.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

Row = dict[str, Any]

REFRESH, FLAG, BASELINE = "refresh", "flag", "baseline"
_OPEN = ("queued",)
_BUILT = ("decomposed",)


class Step(NamedTuple):
    action: str  # REFRESH, FLAG or BASELINE
    link: str
    writes: tuple[Row, ...]  # partial rows for the store's upsert: the keys named, and nothing else, change
    line: str  # what to tell the operator; "" for a baseline, which changes nothing they can see


def source_text(title: str, body: str) -> str:
    """The text a pull files for a ticket: its body, or its title when it has none."""
    return body if body else title


def digest(title: str, body: str) -> str:
    return hashlib.sha256(f"{title}\0{source_text(title, body)}".encode()).hexdigest()[:16]


def _initiative_of(row: Mapping[str, Any], initiatives: Mapping[str, Row]) -> Row | None:
    extra = row.get("extra") or {}
    named = str(extra.get("initiative") or extra.get("id") or row.get("task_id") or "")
    return initiatives.get(named) or initiatives.get(str(row.get("task_id") or ""))


def _key(row: Mapping[str, Any]) -> Row:
    return {"initiative": row["initiative"], "task_id": row["task_id"]}


def plan_sync(candidates: Sequence[Any], rows: Sequence[Row], at: str) -> list[Step]:
    """The steps that bring the store's intake rows up to `candidates`, the source's tickets as read now.
    A candidate no intake row links to is not this function's business: the pull files it."""
    by_link = {str((r.get("extra") or {}).get("link")): r for r in rows if r.get("kind") == "intake" and (r.get("extra") or {}).get("link")}
    initiatives = {str(r["task_id"]): r for r in rows if r.get("kind") == "initiative"}
    steps: list[Step] = []
    for c in candidates:
        row = by_link.get(c.link)
        if row is None:
            continue
        extra = dict(row.get("extra") or {})
        now, seen = digest(c.title, c.body), extra.get("upstream_digest")
        if seen == now or row.get("state") not in (*_OPEN, *_BUILT):
            continue
        remembered = {**_key(row), "extra": {**extra, "upstream_digest": now}}
        text = source_text(c.title, c.body)
        same = str(row.get("title") or "") == c.title and str(row.get("body") or "").strip() == text.strip()
        if row.get("state") in _OPEN:
            if same:
                steps.append(Step(BASELINE, c.link, (remembered,), ""))
                continue
            refreshed = {**remembered, "title": c.title, "body": f"\n{text}\n"}
            steps.append(Step(REFRESH, c.link, (refreshed,), f"sync: {row['task_id']} took the newer text of {c.link}"))
            continue
        initiative = _initiative_of(row, initiatives)
        if seen is None or initiative is None:
            # The first sight of a decomposed ticket has nothing to compare with: remember it, claim no change.
            steps.append(Step(BASELINE, c.link, (remembered,), ""))
            continue
        flagged = {**_key(initiative), "extra": {**(initiative.get("extra") or {}), "changed_upstream": {"at": at, "link": c.link, "title": c.title}}}
        steps.append(Step(
            FLAG, c.link, (remembered, flagged),
            f"sync: {c.link} changed after {initiative['task_id']} was decomposed; its text is kept and the change is recorded on the initiative",
        ))
    return steps
