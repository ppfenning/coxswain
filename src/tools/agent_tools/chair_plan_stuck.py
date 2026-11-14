from __future__ import annotations

from collections.abc import Sequence

from agent_tools.chair_types import Action, Facts

STUCK_TICKS = 3  # consecutive ticks a land failed, or a task sat approved with no run, before the chair acts
RECOVERY_KINDS = ("land_phase", "partial_land", "merge_level")


def failed_recovery_phases(planned: Sequence[Action]) -> frozenset[tuple[str, str]]:
    """The (initiative, phase) pairs of planned recovery actions; held from launches this tick only, the initiative is not."""
    return frozenset(
        (a.get("initiative", ""), a.get("phase", "")) for a in planned if a.get("kind") in RECOVERY_KINDS
    )


def stuck_lands(facts: Facts) -> tuple[list[Action], frozenset[tuple[str, str]]]:
    """One needs_chair per land failing STUCK_TICKS ticks running, and the phases whose land the caller drops."""
    stuck = [s for s in facts.get("land_fail_streaks", []) if s["ticks"] >= STUCK_TICKS]
    actions: list[Action] = [
        {
            "kind": "needs_chair", "initiative": s["initiative"], "phase": s["phase"], "cause": "stuck_land", "epoch": 0,
            "reason": f"initiative {s['initiative']} phase {s['phase']}: {s['kind']} failed {s['ticks']} ticks running: {s['reason']}",
        }
        for s in stuck
    ]
    return actions, frozenset((s["initiative"], s["phase"]) for s in stuck)


def requeue_no_run(facts: Facts) -> list[Action]:
    """One requeue_task per approved task that has had no run to land from for STUCK_TICKS ticks."""
    streaks = facts.get("no_run_streaks", {})
    return [
        {
            "kind": "requeue_task", "initiative": t["initiative"], "phase": t["phase"], "task_id": t["id"],
            "reason": f"approved with no run to land from for {streaks[t['id']]} ticks; rebuilding",
        }
        for t in facts["approved"]
        if streaks.get(t["id"], 0) >= STUCK_TICKS
    ]
