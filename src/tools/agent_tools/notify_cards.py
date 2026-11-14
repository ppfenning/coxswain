"""Pending decision cards as notifier events: `card_events` pure, `pending_cards` the courier.jsonl edge."""

from __future__ import annotations

import json
from pathlib import Path

from agent_tools import courier
from agent_tools.dash_feed import _courier_blob, _open_decisions
from agent_tools.jsonl_log import parse_log
from agent_tools.notify_core import Event, render

__all__ = ["card_events", "pending_cards"]

# The feed's private readers, reused so "pending" has one definition.


def _event(card: dict) -> Event:
    question = str(card.get("question") or "")
    title, body, _ = render(Event("decision_card", "", question, question))
    return Event("decision_card", f"decision_card:{card['id']}", title, body)


def card_events(cards: list[dict]) -> list[Event]:
    """One event per card without an answer."""
    return [_event(card) for card in cards if not card.get("answer")]


def _blob(rows: tuple[dict, ...]) -> str:
    """One json line per parsed row, so `_open_decisions` never meets a damaged line and hides every card."""
    return "".join(f"{json.dumps(row)}\n" for row in rows)


def pending_cards(workspace) -> list[dict]:
    """Open decisions in `<workspace>/courier.jsonl`; [] when there are none or the read fails."""
    try:
        blob = _blob(parse_log(_courier_blob(Path(workspace))).rows)
        return _open_decisions(courier.inbox(blob), blob)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return []
