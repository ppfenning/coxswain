"""What a queue worker pushes after each pass (C6): the tick's push, split by who planned what.

Before the cutover the tick's notify step pushed every needs-you event (`chair_run._notify_step`); after it the tick
is stopped, so each worker pushes the events of its own pass. What is the chair's as a whole, the open decision cards
and `queue_empty`, is pushed by the worker that raises the queue alarms (`chair_kinds.watcher`), and by none while a
tick holds the chair lease, since the tick pushes them itself.

Each kind keeps its own sent file (`notify-sent-<kind>.json`): `notify_core.due` reads a key that has left a call's
events as cleared, so two processes sharing one file would push each other's standing keys again.

Pure; the edge (`cli_chair_service._chair_worker`) reads the cards and calls the notifier.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import PurePath

from agent_tools import chair_kinds
from agent_tools.chair_types import Action, Facts
from agent_tools.notify_core import Event
from agent_tools.notify_events import QUEUE_EMPTY, events_from_tick

__all__ = ["sent_path", "worker_events"]


def sent_path[P: PurePath](workspace: P, kind: str) -> P:
    """The sent file of `kind`'s worker, beside the tick's `notify-sent.json`."""
    return workspace / f"notify-sent-{kind}.json"


def worker_events(kind: str, facts: Facts, actions: Sequence[Action], cards: Sequence[Event]) -> list[Event]:
    """Pure. The events the worker of `kind` pushes after a pass that planned `actions`: those of its own actions, and,
    when it is the watcher, `queue_empty` and the open decision cards."""
    watching = chair_kinds.watcher(kind, facts)
    own = [e for e in events_from_tick(facts, list(actions)) if watching or e.key != QUEUE_EMPTY.key]
    return [*own, *(cards if watching else [])]
