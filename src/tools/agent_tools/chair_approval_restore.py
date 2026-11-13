"""Plan a restore for the tasks the approval hold released while merged on their phase branch.

Pure rules over the tick's facts. Nothing here reads a file, a clock or the store.
"""

from agent_tools.chair_types import Action, Facts

REASON = "released by the approval hold while merged on its phase branch; restoring to approved"


def plan_restore(facts: Facts) -> list[Action]:
    """One restore_approvals action per initiative in `wrongly_released`, task_ids in facts order."""
    entries = facts.get("wrongly_released", [])
    return [
        {
            "kind": "restore_approvals",
            "initiative": initiative,
            "task_ids": [e["task_id"] for e in entries if e["initiative"] == initiative],
            "reason": REASON,
        }
        for initiative in dict.fromkeys(e["initiative"] for e in entries)
    ]


def not_released(restores: list[Action], releases: list[Action]) -> list[Action]:
    """The restores without any task a release names for the same initiative; a restore left empty is dropped."""
    released = {(a["initiative"], t) for a in releases for t in a.get("task_ids", [])}
    trimmed = [
        {**a, "task_ids": [t for t in a.get("task_ids", []) if (a["initiative"], t) not in released]}
        for a in restores
    ]
    return [a for a in trimmed if a["task_ids"]]
