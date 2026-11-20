"""Pure core for the service sorting its own quarantines. Imports no I/O module; the edge supplies every input.

A task state is rebuilt once and an objection annotated once. `handled_keys` holds both kinds of key, written by the caller.
"""

import hashlib
import re
from collections.abc import Collection
from typing import Literal, Protocol

from agent_tools.chair_types import QuarantineFacts

Sort = Literal["rebuild_on_main", "annotate_objection", "needs_attention", "skip"]

# QuarantineFacts.cause is a coarse label, not a subcause. "harness" equals chair_facts.HARNESS_CAUSE and covers a stale
# commit, a stale base, an empty build and an interrupted run alike; one rebuild answers all of them.
# "review" is the cause label for a review, adversary or arbitration verdict (graphs cause_rule.Cause); the objection
# text is the fact's `reason`. A wrong belief to avoid: there is no "stale_base" or "review_objection" cause value.
HARNESS_CAUSE = "harness"
OBJECTION_CAUSE = "review"

NOTE_HEADING = "## Rebuild note"
MAX_NOTE = 600
MAX_OBJECTION_IN_NOTE = 200


class ObjectionNote(Protocol):
    """The injected cheap model step: the objection text in, a short rebuild instruction out. The core never calls it."""

    def __call__(self, objection: str) -> str: ...


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def state_key(task_id: str, task_state_key: str) -> str:
    """The handled key for a harness state."""
    return f"{task_id}:{task_state_key}"


def objection_key(task_id: str, reason: str) -> str:
    """A stable hash of the task id and the whitespace and case normalised objection text."""
    return hashlib.sha256(f"{task_id}\0{_squash(reason).lower()}".encode()).hexdigest()


def triage_key(task_id: str, run: str) -> str:
    """The handled key for a triage request: one per task and the run that quarantined it."""
    return f"triage:{task_id}:{run}"


def rescue_key(task_id: str, run: str) -> str:
    """The handled key for a rescue of a kept patch: one per task and the run that quarantined it."""
    return f"rescue:{task_id}:{run}"


def hold_key(task_id: str, run: str) -> str:
    """The handled key for the chair's hold of a quarantined task: one per task and the run that quarantined it."""
    return f"hold:{task_id}:{run}"


def announce_key(task_id: str, reason: str) -> str:
    """The handled key for a held task's one courier entry: a new hold reason on the same task is announced again."""
    return f"announced:{task_id}:{hashlib.sha256(_squash(reason).encode()).hexdigest()[:16]}"


def classify(q: QuarantineFacts, task_state_key: str, handled_keys: Collection[str]) -> Sort:
    cause = q["cause"]
    if cause == HARNESS_CAUSE:
        return "skip" if state_key(q["task_id"], task_state_key) in handled_keys else "rebuild_on_main"
    if cause == OBJECTION_CAUSE:
        reason = q.get("reason", "")
        # A blank objection has nothing to annotate, so it goes to a person rather than being hashed as "".
        if not _squash(reason):
            return "needs_attention"
        return "needs_attention" if objection_key(q["task_id"], reason) in handled_keys else "annotate_objection"
    return "needs_attention"


def _cut(text: str, limit: int) -> str:
    """`text` whole when it fits; otherwise cut at a word boundary and ended with an ellipsis, within `limit`."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip() + "…"


def rebuild_note(reason: str, cheap_model_text: str) -> str:
    """The note appended to a ticket body: NOTE_HEADING, the objection cut to 200, then the instruction in what is
    left of MAX_NOTE. The heading and the start of the instruction always survive."""
    head = f"{NOTE_HEADING}\n\nObjection: {_cut(_squash(reason), MAX_OBJECTION_IN_NOTE)}\n\nInstruction: "
    return head + _cut(_squash(cheap_model_text), MAX_NOTE - len(head))
