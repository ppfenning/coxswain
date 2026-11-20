"""Plan the stale half of a chair tick: one stale_to_draft action per initiative with a newly stale task.

Pure. Takes the facts and the tick's clock, returns actions. No file, store or clock access of its own.
"""
from datetime import datetime

from agent_tools.chair_stale import stale_reason
from agent_tools.chair_types import Action, ActionKind, Facts, StaleCandidate

_Row = tuple[str, str, str]  # initiative, task_id, reason


def _already_drafted(facts: Facts) -> frozenset[str]:
    """Initiative ids that already carry a draft flag in facts['initiatives'].

    No such field exists in InitiativeFacts yet, so this checks a conventionally named
    'draft' key defensively: a no-op today, and live the day the facts edge adds one.
    """
    return frozenset(i["id"] for i in facts["initiatives"] if i.get("draft", False))


def _held(facts: Facts) -> frozenset[str]:
    """Initiatives a person already decides: drafting one moves nothing, and the board export restores its files."""
    return frozenset(facts.get("held_initiatives", []))


def draft_key(initiative: str, stale_tasks: list[str]) -> str:
    """`<initiative>|<sorted stale task ids>`: one stale_to_draft per stale set, however many ticks it stays stale."""
    return f"{initiative}|{','.join(sorted(stale_tasks))}"


def _reason(row: StaleCandidate, stale_days: int, now_iso: str) -> str | None:
    return stale_reason(
        row["state"],
        row["last_file_change"],
        row["last_run"],
        row["last_chair_action"],
        row["quarantine_non_harness_count"],
        stale_days,
        now_iso,
    )


def plan_stale(facts: Facts, now: datetime) -> list[Action]:
    """One stale_to_draft per initiative with at least one stale row, in the order first seen. None for a held
    initiative, nor for one whose stale set already has a settled stale_to_draft in the action log (`stale_drafts_reported`):
    done, or refused as held or as one the board export would restore, which every later tick would only repeat."""
    excluded = _already_drafted(facts) | _held(facts)
    reported = frozenset(facts.get("stale_drafts_reported", []))
    now_iso = now.isoformat()
    stale_days = facts.get("stale_days", 7)
    candidates = [row for row in facts.get("stale_candidates", []) if row["initiative"] not in excluded]
    stale_rows: list[_Row] = [
        (row["initiative"], row["task_id"], reason)
        for row in candidates
        for reason in (_reason(row, stale_days, now_iso),)
        if reason is not None
    ]
    order = list(dict.fromkeys(initiative for initiative, _, _ in stale_rows))
    stale_sets = {initiative: [task_id for i, task_id, _ in stale_rows if i == initiative] for initiative in order}
    return [
        {
            "kind": ActionKind.STALE_TO_DRAFT,
            "initiative": initiative,
            "stale_tasks": stale_sets[initiative],
            "reason": next(reason for i, _, reason in stale_rows if i == initiative),
            "since": now_iso,
        }
        for initiative in order
        if draft_key(initiative, stale_sets[initiative]) not in reported
    ]
