"""The routine actions a chair plans while the lease holder has lent its routine ticks."""
from agent_tools.chair_types import Action

ROUTINE_KINDS = frozenset(
    {
        "relaunch", "clear_branches", "rebase_phase", "fetch", "fetch_exit", "land_phase", "needs_chair", "waiting",
        "merge_level", "run_gate", "steward_event",
    }
)


def routine_only(actions: list[Action]) -> list[Action]:
    """Keep, in order, the kinds in ROUTINE_KINDS; launches, retries, rescues and the operator's own work are dropped."""
    return [a for a in actions if a["kind"] in ROUTINE_KINDS]
