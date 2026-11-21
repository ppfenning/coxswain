"""The new model's merge hold: set when a phase merge cannot be made, and how it is lifted. All pure.

A new-model phase merge that the merge agent cannot settle, or that fails the guard or the level checks, holds the
initiative (`merge_hold`). The hold records its phase, the tip it could not merge, its cause and how many times that
merge was tried. It is lifted in one of three ways, all decided here and acted on by the sweep:

- origin's branch of the phase moves to a new tip: a fix was pushed (`hold_lifted`);
- an operator releases it (`cox lander release`, `released`);
- the automatic retry comes due (`retry_due`). A resolver that raised, or a harness fault, is retried up to
  `MERGE_RETRIES` times with a doubling backoff. "No merge agent yet" is retried every `NO_AGENT_RETRY_SECONDS`
  without a bound: each try costs one scratch merge and a refusal, and the first pass after L5 wires the agent
  settles it. A clean refusal, a failed level check and a guard refusal are never retried on their own: they need a
  new tip or a person.

A hold lifted for a retry leaves its count on the record (`retried`), so the next hold of the same phase at the same
tip counts on from it (`next_hold`). Reconciliation never replaces a merge hold: a drift it finds is kept beside it
(`drift`) and cleared on its own (`reconciled_hold`).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal

from core import landing_record as lr

# The prefix of the hold a new-model phase merge sets when it cannot be made.
MERGE_HOLD = "the lander could not merge new-model phase "
NO_MERGE_AGENT = "no merge agent yet: this lander has no merge agent wired, so a conflict on the new model holds"

MergeCause = Literal["refused", "no_merge_agent", "resolver_error", "harness_fault", "checks_failed", "guard"]
RETRIED: frozenset[str] = frozenset({"resolver_error", "harness_fault"})  # bounded automatic retry
MERGE_RETRIES = 3  # automatic retries of one phase at one tip after a resolver error or a harness fault
RETRY_BASE_SECONDS = 600  # the first retry's backoff; each next one doubles (10m, 20m, 40m)
NO_AGENT_RETRY_SECONDS = 3600  # "no merge agent yet" is tried again hourly, unbounded, until an agent answers

_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def _later(stamp: str, seconds: int) -> str | None:
    try:
        moment = datetime.strptime(stamp, _STAMP).replace(tzinfo=UTC)
    except ValueError:
        return None
    return (moment + timedelta(seconds=seconds)).strftime(_STAMP)


def merge_cause(cause: str, unsettled: str, detail: str) -> MergeCause:
    """Pure. Why a new-model phase merge was held, from `ops.PhaseMerge`'s `cause` (`conflict`, `checks_failed`,
    `guard`) and `unsettled` (`lg.Unsettled`: the resolver refused, raised, or the harness could not commit)."""
    if cause in ("checks_failed", "guard"):
        return "checks_failed" if cause == "checks_failed" else "guard"
    if unsettled == "raised":
        return "resolver_error"
    if unsettled == "fault":
        return "harness_fault"
    return "no_merge_agent" if NO_MERGE_AGENT in detail else "refused"


def is_merge_hold(hold: Mapping[str, object] | None) -> bool:
    """Pure. Whether `hold` is a merge hold (`merge_hold`)."""
    return str((hold or {}).get("reason") or "").startswith(MERGE_HOLD)


def merge_held(record: lr.LandingRecord) -> bool:
    """Pure. Whether the record is held by a merge hold, which only the sweep or a person lifts."""
    return is_merge_hold(record.get("held"))


def merge_hold(phase: str, tip: str, detail: str, now: str, cause: MergeCause = "refused", attempts: int = 1) -> lr.Hold:
    """Pure. The hold a new-model phase merge sets when it cannot be made: the merge agent could not settle its
    conflict, or the merge (resolved or clean) failed the guard or the level checks. The reason, the phase, the tip,
    the cause, and the number of times this merge has now been tried and held."""
    return {"reason": f"{MERGE_HOLD}{phase} at {tip[:12]}: {detail}", "since": now, "phase": phase, "tip": tip,
            "cause": cause, "attempts": attempts, "last_attempt": now}


def next_hold(prior: lr.Hold | None, phase: str, tip: str, detail: str, now: str, cause: MergeCause) -> lr.Hold:
    """Pure. The merge hold to set now, counting on from `prior` (the hold this record still carries, or the one
    lifted for a retry) when it held the same phase at the same tip; a new phase or tip starts the count again."""
    same = prior is not None and prior.get("phase") == phase and prior.get("tip") == tip
    attempts = int((prior or {}).get("attempts") or 1) + 1 if same else 1
    hold = merge_hold(phase, tip, detail, now, cause, attempts)
    return {**hold, "since": str((prior or {}).get("since") or now)} if same else hold


def retry_delay(cause: str, attempts: int) -> int | None:
    """Pure. Seconds after a merge hold's last attempt before it is tried again on its own, or None when it never is.
    Attempt `n` is followed by retry `n` while `n <= MERGE_RETRIES`, so a phase is tried at most `MERGE_RETRIES + 1`
    times at one tip."""
    if cause == "no_merge_agent":
        return NO_AGENT_RETRY_SECONDS
    if cause in RETRIED and 1 <= attempts <= MERGE_RETRIES:
        return RETRY_BASE_SECONDS * 2 ** (attempts - 1)
    return None


def retry_due(hold: Mapping[str, object] | None, now: str) -> bool:
    """Pure. Whether a merge hold is to be lifted and the merge tried again on this pass: an operator released it, or
    its cause is retried and its backoff since the last attempt has passed. A hold with no recorded cause (made before
    causes were) is not retried on its own."""
    if not is_merge_hold(hold):
        return False
    held = hold or {}
    if held.get("released") is True:
        return True
    attempts = held.get("attempts")
    delay = retry_delay(str(held.get("cause") or ""), attempts if isinstance(attempts, int) else 1)
    due = _later(str(held.get("last_attempt") or held.get("since") or ""), delay) if delay is not None else None
    return due is not None and due <= now


def retries_spent(hold: Mapping[str, object]) -> bool:
    """Pure. Whether a hold whose cause is retried automatically has had its last automatic retry."""
    attempts = hold.get("attempts")
    return str(hold.get("cause") or "") in RETRIED and retry_delay(
        str(hold.get("cause")), attempts if isinstance(attempts, int) else 1) is None


def hold_lifted(record: lr.LandingRecord, origin_tip: str | None) -> bool:
    """Pure. Whether a merge hold is lifted: origin's branch of the held phase is at another tip than the one the
    merge agent could not merge (a fix was pushed; the next merge tries it). A phase branch origin lacks lifts nothing."""
    held = record.get("held") or {}
    return merge_held(record) and bool(origin_tip) and origin_tip != str(held.get("tip") or "")


def without_merge_hold(hold: lr.Hold | None, now: str) -> lr.Hold | None:
    """Pure. What is left once a merge hold is lifted: a drift reconciliation kept beside it, as a hold of its own,
    or nothing."""
    drift = str((hold or {}).get("drift") or "")
    return {"reason": drift, "since": now} if drift else None


def told_now(prior: lr.Hold | None, hold: lr.Hold, same_reason: bool) -> bool:
    """Pure. Whether setting `hold` over `prior` is news for the chair: a first hold, a new reason, an answer to an
    operator's release, or the last automatic retry spent. A retry that fails the same way again is not told twice."""
    if prior is None or not same_reason:
        return True
    return prior.get("released") is True or retries_spent(hold)


def release(record: lr.LandingRecord) -> tuple[lr.LandingRecord | None, str]:
    """Pure. The record with its merge hold released for the next pass (`retry_due`), or None and why not. Only a
    merge hold is released here: a record and branch that disagree are reconciliation's to settle."""
    initiative = str(record.get("initiative") or "")
    hold = record.get("held")
    if not hold:
        return None, f"{initiative} is not held"
    if not is_merge_hold(hold):
        return None, (f"{initiative} is held, but not by a merge: {hold.get('reason')}; "
                      "only reconciliation lifts that, once the record and origin agree")
    if hold.get("released") is True:
        return None, f"{initiative}: its merge hold is already released; the next pass tries phase {hold.get('phase')} again"
    released: lr.Hold = {**hold, "released": True}
    return {**record, "held": released}, (
        f"{initiative}: the merge hold on phase {hold.get('phase')} is released; the next lander pass tries it again "
        f"(cause {hold.get('cause') or 'unrecorded'}, {hold.get('attempts') or 1} attempt(s) so far)")


def reconciled_hold(held: lr.Hold | None, reason: str | None, now: str) -> tuple[lr.Hold | None, bool] | None:
    """Pure. A merge hold once reconciliation found `reason` (None: the record and origin agree): the drift is kept
    beside the merge hold (`drift`), never in its place, and a drift that clears takes only itself away. (the hold,
    whether that is news). None when `held` is not a merge hold: `lander.held_after` decides that."""
    if not is_merge_hold(held):
        return None
    hold = lr.Hold(**(held or {}))
    drift = str(hold.get("drift") or "")
    if reason is None:
        if not drift:
            return hold, False
        hold.pop("drift", None)
        return hold, True
    if reason == drift:
        return hold, False
    return {**hold, "drift": reason}, True
