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

The gate lifts a review hold once it no longer says the same hold: the tip moved, or the rounds moved on. A quarantine
holds only at the tip its round reviewed: once the branch moves (the fix triage sent to build has merged) the gate
waits, and the chair queues the review again (`chair_initiative_review`). An old-model record is never gated: it
lands as it always has.

The tip the gate approved is the only tip a pull request opens at. The land then takes the parent forward
(`_open_pr`), and a phase merge may land on origin meanwhile, so just before the push and `open_pr`, under the
branch's lease, the land reads both again (`refresh_step`): at the approved tip it opens; moved, it pushes the
refreshed branch when origin still holds the approved tip, records the move (`moved`) and waits, and the chair
queues the review and the suite again at the new tip. A parent that moves faster than a suite runs would loop
for ever, so `MOVE_LIMIT` moves in a row hold the pull request (`outpaced`), the chair is told, and only an
operator's release lifts that hold. Every tip the gate judges is kept on the record (`review_tip`): the chair reads it.

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

__all__ = ["MOVE_LIMIT", "Decision", "Gate", "Refresh", "decide", "moved", "pr_gate", "pr_note", "refresh_step", "settle"]

MOVE_LIMIT = 3  # tip moves in a row between a passing suite and the pull request before the gate holds it


@dataclass(frozen=True)
class Decision:
    """What the gate says, why, and for a hold its cause."""

    gate: Gate
    reason: str
    cause: lander_hold.ReviewCause | None = None
    tip: str = ""  # the tip the gate judged: for `open`, the one the suite passed at and the pull request opens at


def pr_gate(record: lr.LandingRecord, rounds: Sequence[review_loop.Round],
            suite_step: initiative_review.SuiteSeen | None, tip: str, reviewed: str = "") -> Gate:
    """Pure. Whether the initiative's one pull request opens now: `open` only when the current episode's last
    initiative round approved and a passing `suite` step stands at `tip`, the initiative branch's current tip (`suite_step`
    is the last suite at `tip`, `initiative_review.suite_at`; None when none ran there); `hold` when the review
    quarantined it at `tip` (`reviewed`, the tip its last round reviewed; "" when unknown), or the suite failed at
    `tip`; else `wait`. An old-model record is always `open`."""
    if lr.record_lane_model(record) != lr.NEW_MODEL:
        return "open"
    held = review_loop.episode(rounds)
    last = held[-1] if held else None
    if last is not None and last.verdict == "quarantine":
        return "hold" if reviewed in ("", tip) else "wait"
    if last is None or last.verdict != "approve" or suite_step is None or suite_step.tip != tip:
        return "wait"
    return "open" if suite_step.ok else "hold"


def decide(record: lr.LandingRecord, rounds: Sequence[review_loop.Round],
           suite_step: initiative_review.SuiteSeen | None, tip: str, reviewed: str = "") -> Decision:
    """Pure. `pr_gate`'s answer, with why, and for a hold its cause, as the land and the chair are told it."""
    decision = _decide(record, rounds, suite_step, tip, reviewed)
    return Decision(decision.gate, decision.reason, decision.cause, tip)


def _decide(record: lr.LandingRecord, rounds: Sequence[review_loop.Round],
            suite_step: initiative_review.SuiteSeen | None, tip: str, reviewed: str) -> Decision:
    gate = pr_gate(record, rounds, suite_step, tip, reviewed)
    held = review_loop.episode(rounds)
    last = held[-1] if held else None
    if lr.record_lane_model(record) != lr.NEW_MODEL or last is None:
        return Decision(gate, "" if gate == "open" else "the initiative review has not decided a round yet")
    if last.verdict == "quarantine" and gate == "wait":
        return Decision(gate, f"the initiative review quarantined it in round {last.number} at {reviewed[:12]}; the "
                              f"branch has moved to {tip[:12]} since, and the review runs again there")
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
    """Pure. The line the pull request's body carries for the gate it passed, naming in full the sha the suite passed
    at (the one the pull request opens at, `refresh_step`); '' for an old-model record."""
    if decision.gate != "open" or not decision.reason:
        return ""
    return f"Initiative review: {decision.reason}." + (f" The full suite passed at `{decision.tip}`." if decision.tip else "")


@dataclass(frozen=True)
class Refresh:
    """What the land does just before its push and `open_pr` on the new model (`refresh_step`): `open` at the approved
    tip; `wait` for the review and the suite at the new tip (`push`: publish the refreshed branch first); or `hold`,
    the branch outpacing the suite."""

    action: Literal["open", "wait", "hold"]
    push: bool
    reason: str


def refresh_step(approved: str, local: str | None, origin: str | None, moves: int, limit: int = MOVE_LIMIT) -> Refresh:
    """Pure. The new model's last word before the pull request. `approved` is the tip the gate opened at (the suite
    passed there), `local` the initiative branch once the parent was taken forward, `origin` origin's copy read again
    under the branch's lease (a phase merge may have landed since the gate read it), `moves` the moves in a row already
    recorded (`review_tip`). The pull request opens only when both are still `approved`. Else the branch moved: the
    refreshed branch is pushed when origin still holds `approved` (only the parent merge moved it; when origin moved
    itself, a push would be refused and the next pass fetches what it holds), and the land waits; the `limit`-th move in
    a row holds instead."""
    if local == approved and origin == approved:
        return Refresh("open", False, f"the branch is at {approved[:12]}, where the suite passed")
    push = origin == approved and bool(local)
    now_at = (local if push else origin) or "an unread tip"
    count = moves + 1
    if count >= limit:
        return Refresh("hold", push, f"the branch moved {count} times in a row between a passing suite and its pull "
                                     f"request, now to {now_at[:12]}: its parent moves faster than the suite runs")
    return Refresh("wait", push, f"the branch moved from {approved[:12]} to {now_at[:12]} after the suite passed "
                                 f"(move {count} of {limit}): the review and the suite run again there")


def _review_tip(record: lr.LandingRecord) -> lr.ReviewTip:
    found = record.get("review_tip")
    return found if isinstance(found, dict) else {}


def moved(conn: Connection, record: lr.LandingRecord, step: Refresh, tip: str, now: str,
          notify: Callable[[str, Mapping[str, object]], None]) -> list[str]:
    """Edge. Record a move `refresh_step` found: the new tip and one more move in a row on `review_tip`, so the chair
    queues the review again there; a `hold` also sets the `outpaced` review hold and tells the chair once."""
    initiative = str(record.get("initiative") or "")
    current = ls.get_record(conn, initiative) or record
    moves = int(_review_tip(current).get("moves") or 0) + 1
    updated: lr.LandingRecord = {**current, "review_tip": {"tip": tip, "moves": moves}, "updated_at": now}
    events = [f"{initiative}: no pull request at the old tip: {step.reason}"]
    if step.action == "hold" and not updated.get("held"):
        prior = current.get("retried") if lander_hold.is_review_hold(current.get("retried")) else None
        hold = lander_hold.review_hold(prior, tip, step.reason, now, "outpaced")
        updated = {**updated, "held": hold, "retried": None}
        notify("needs_chair", {"initiative": initiative, "reason": f"held: {hold.get('reason')}"})
        events += [f"{initiative} is held: {hold.get('reason')}", f"{initiative}: the chair has been told"]
    ls.put_record(conn, updated)
    return events


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
    round_rows = store_phases.review_rounds(conn, initiative, initiative_review.ROUNDS_PHASE)
    rounds = review_loop.rounds_of(round_rows)
    suite = initiative_review.suite_at(store_phases.suite_rows(conn, initiative), tip)
    decision = decide(record, rounds, suite, tip, initiative_review.reviewed_tip(round_rows))
    current: lr.LandingRecord = ls.get_record(conn, initiative) or record
    if _review_tip(current).get("tip") != tip:  # the tip the chair reads: a review that judged another runs again
        current = {**current, "review_tip": {**_review_tip(current), "tip": tip}, "updated_at": now}
        ls.put_record(conn, current)
    held = current.get("held") or None
    events: list[str] = []
    retried = current.get("retried")
    prior: lr.Hold | None = retried if lander_hold.is_review_hold(retried) else None
    if lander_hold.is_review_hold(held):
        released = (held or {}).get("released") is True
        if (held or {}).get("cause") == "outpaced" and not released:  # only an operator's release lifts it
            return current, Decision("hold", str((held or {}).get("reason") or ""), "outpaced", tip), events
        same = decision.gate == "hold" and (held or {}).get("cause") == decision.cause and (held or {}).get("tip") == tip
        if same and not released:
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
