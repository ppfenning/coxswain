"""D2: the new model's pull-request gate. A new-model initiative's one pull request opens only once the initiative
review has approved it and the full suite has passed at the initiative branch's current tip.

The review is the phase lane's (`harness.initiative_review`): a worker reviews the assembled initiative branch as a
whole (its `review_rounds`, phase `''`) and, on `approve`, runs the `ticket` gate there as one `suite` step row.
`pr_gate` reads those facts and says what the land does:

- `open`: the current episode's last round approved, and a passing `suite` row stands at the current tip;
- `hold`: the episode's last round quarantined the initiative, or the suite failed at the current tip: new work or a
  person must move it. The hold is a review hold (`lander_hold.review_hold`), which keeps D1's rules: it records its
  tip and cause, the chair is told once, reconciliation keeps a drift beside it, `cox lander release` releases it and
  the next pass decides again and tells the answer;
- `wait`: anything else: no round yet, a revise whose revision phase is still being built and merged, or an approval
  whose suite has not run at this tip (a revision merged since, or the lane has not got there).

The gate lifts a review hold once it no longer says the same hold: the tip moved, or the rounds moved on. An old-model
record is never gated: it lands as it always has.

Which tip: origin's copy of the initiative branch, which the lander pushes before it records each phase merge, and
which the review and the suite read. `land_checks: ci` adds CI on the pull request; it does not stand in for the
suite: the suite has passed at this tip before the pull request exists, and CI runs again once it does.

`decide` and `pr_gate` are pure; `settle` is the edge.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from agent_tools import lander_hold
from core import landing_record as lr
from harness import initiative_review, review_loop, store_phases
from harness import landing_store as ls
from harness.store_dialect import Connection

Gate = Literal["open", "wait", "hold"]

__all__ = ["Decision", "Gate", "decide", "pr_gate", "pr_note", "settle"]


@dataclass(frozen=True)
class Decision:
    """What the gate says, why, and for a hold its cause."""

    gate: Gate
    reason: str
    cause: lander_hold.ReviewCause | None = None


def pr_gate(record: lr.LandingRecord, rounds: Sequence[review_loop.Round],
            suite_step: initiative_review.SuiteSeen | None, tip: str) -> Gate:
    """Pure. Whether the initiative's one pull request opens now: `open` only when the current episode's last
    initiative round approved and a passing `suite` step stands at `tip`, the initiative branch's current tip (`suite_step`
    is the last suite at `tip`, `initiative_review.suite_at`; None when none ran there); `hold` when the review
    quarantined it, or the suite failed at `tip`; else `wait`. An old-model record is always `open`."""
    if lr.record_lane_model(record) != lr.NEW_MODEL:
        return "open"
    held = review_loop.episode(rounds)
    last = held[-1] if held else None
    if last is not None and last.verdict == "quarantine":
        return "hold"
    if last is None or last.verdict != "approve" or suite_step is None or suite_step.tip != tip:
        return "wait"
    return "open" if suite_step.ok else "hold"


def decide(record: lr.LandingRecord, rounds: Sequence[review_loop.Round],
           suite_step: initiative_review.SuiteSeen | None, tip: str) -> Decision:
    """Pure. `pr_gate`'s answer, with why, and for a hold its cause, as the land and the chair are told it."""
    gate = pr_gate(record, rounds, suite_step, tip)
    held = review_loop.episode(rounds)
    last = held[-1] if held else None
    if lr.record_lane_model(record) != lr.NEW_MODEL or last is None:
        return Decision(gate, "" if gate == "open" else "the initiative review has not decided a round yet")
    if last.verdict == "quarantine":
        return Decision(gate, f"the initiative review quarantined it in round {last.number}"
                              + (f": {last.reason}" if last.reason else ""), "quarantined")
    if last.verdict == "revise":
        return Decision(gate, f"the initiative review's round {last.number} asked for "
                              f"{initiative_review.revision_phase(last.number)}, which has not been reviewed again")
    if last.verdict != "approve":
        return Decision(gate, f"the initiative review's round {last.number} said {last.verdict}")
    if suite_step is None or suite_step.tip != tip:
        return Decision(gate, f"the initiative review approved it in round {last.number}; the suite has not run at "
                              f"{tip[:12]}")
    if gate == "hold":
        return Decision(gate, f"the suite failed at {tip[:12]}: "
                              f"{suite_step.reason.removeprefix(initiative_review.SUITE_FAILED)}", "suite_failed")
    return Decision(gate, f"the initiative review approved it in round {last.number} and the suite passed at {tip[:12]}")


def pr_note(decision: Decision) -> str:
    """Pure. The line the pull request's body carries for the gate it passed; '' for an old-model record."""
    return f"Initiative review: {decision.reason}." if decision.gate == "open" and decision.reason else ""


def settle(conn: Connection, record: lr.LandingRecord, tip: str, now: str,
           notify: Callable[[str, Mapping[str, object]], None]) -> tuple[lr.LandingRecord, Decision, list[str]]:
    """Edge. Decide the gate at `tip` from the store (`decide`), and bring the record's review hold into line with it.
    A review hold the gate no longer says, or one an operator released, is lifted (a drift kept beside it stays); a
    `hold` then sets one, counting on from the last at this tip for this cause, and the chair is told when that is news
    (`lander_hold.told_now`: a first hold, a new reason, the answer to a release). Another hold (a drift, a merge hold)
    is left alone: the land waits on it first. (the record as it now stands, the decision, events)."""
    initiative = str(record.get("initiative") or "")
    if lr.record_lane_model(record) != lr.NEW_MODEL:
        return record, Decision("open", ""), []
    rounds = review_loop.rounds_of(store_phases.review_rounds(conn, initiative, initiative_review.ROUNDS_PHASE))
    suite = initiative_review.suite_at(store_phases.suite_rows(conn, initiative), tip)
    decision = decide(record, rounds, suite, tip)
    current = ls.get_record(conn, initiative) or record
    held = current.get("held") or None
    events: list[str] = []
    retried = current.get("retried")
    prior: lr.Hold | None = retried if lander_hold.is_review_hold(retried) else None
    if lander_hold.is_review_hold(held):
        same = decision.gate == "hold" and (held or {}).get("cause") == decision.cause and (held or {}).get("tip") == tip
        if same and (held or {}).get("released") is not True:
            return current, decision, events
        lifted: lr.LandingRecord = {**current, "held": lander_hold.without_merge_hold(held, now), "retried": None,
                                    "updated_at": now}
        ls.put_record(conn, lifted)
        current = lifted
        why = "an operator released it" if (held or {}).get("released") is True else decision.reason
        events.append(f"{initiative}: the initiative review hold is lifted: {why}")
        prior, held = held, current.get("held") or None
    if decision.gate != "hold" or decision.cause is None or held:
        return current, decision, events
    hold = lander_hold.review_hold(prior, tip, decision.reason, now, decision.cause)
    news = lander_hold.told_now(prior, hold, str((prior or {}).get("reason") or "") == str(hold.get("reason") or ""))
    holding: lr.LandingRecord = {**current, "held": hold, "retried": None, "updated_at": now}
    ls.put_record(conn, holding)
    events.append(f"{initiative} is held: {hold.get('reason')}")
    if news:
        notify("needs_chair", {"initiative": initiative, "reason": f"held: {hold.get('reason')}"})
        events.append(f"{initiative}: the chair has been told")
    return holding, decision, events
