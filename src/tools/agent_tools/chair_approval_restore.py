"""Plan a restore for the tasks the approval hold released while merged on their phase branch.

Pure rules over the tick's facts. Nothing here reads a file, a clock or the store.
"""

from agent_tools.chair_types import Action, ActionKind, Facts

REASON = "released by the approval hold while merged on its phase branch; restoring to approved"


def plan_restore(facts: Facts) -> list[Action]:
    """One restore_approvals action per initiative in `wrongly_released`, task_ids in facts order."""
    entries = facts.get("wrongly_released", [])
    return [
        {
            "kind": ActionKind.RESTORE_APPROVALS,
            "initiative": initiative,
            "task_ids": [e["task_id"] for e in entries if e["initiative"] == initiative],
            "reason": REASON,
        }
        for initiative in dict.fromkeys(e["initiative"] for e in entries)
    ]
