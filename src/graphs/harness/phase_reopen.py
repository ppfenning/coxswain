"""`cox worker reopen`: the operator's retry of a new-model phase that finished `failed` or `quarantined`.

A phase that finished failed (its claims ran out, or its smoke failed) is terminal: the chair reopens only a phase
that finished approved or quarantined and is wanted again (`chair_phase_queue._REOPENED`), so a failed smoke never
loops on its own. The operator retries it here, on purpose and with a reason. The row goes back to `ready` with its
attempts at 0, claimable at once, and a reopen from `finished` ends the last review episode (`store_phases._apply`'s
`reopen`, which writes the L6 `reopened` round when the phase has rounds), so the next lane reviews afresh. Nothing on
the phase branch is touched: the next claim resumes from the tasks' trailers there (`lane_progress.resume_plan`) and
builds only what is missing.

`verdict` decides on the row as the store holds it, locked (`store_phases.reopen_phase`), so the write is the
decision's: a claimed row, an approved one and a phase with no queue row (not on the new model) are refused; a row
already `ready` is left as it is, so a repeat changes nothing. Everything here is pure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, cast

__all__ = ["REOPENABLE", "Verdict", "change", "note", "reason", "release_hints", "target", "verdict"]

REOPENABLE = frozenset({"failed", "quarantined"})  # the outcomes an operator may reopen

# reopen: finished failed or quarantined, so it goes back to ready. already: it is ready; nothing to do. absent: no
# queue row, so no phase of the new model. claimed: a worker holds it. approved: it finished approved. other: any
# other state or outcome (waiting, built, withdrawn, an initiative review finished revised), which the chair moves.
Verdict = Literal["reopen", "already", "absent", "claimed", "approved", "other"]

type Row = Mapping[str, object]


def target(text: str) -> tuple[str, str] | None:
    """`<initiative>/<phase>` as (initiative, phase), split at the last `/`; None when either side is empty."""
    initiative, _, phase = text.strip().rpartition("/")
    return (initiative, phase) if initiative and phase else None


def verdict(row: Row | None) -> Verdict:
    """What `cox worker reopen` does with the queue row `row` (None when the phase has none)."""
    if row is None:
        return "absent"
    state, outcome = row.get("state"), row.get("outcome")
    if state == "ready":
        return "already"
    if state == "claimed":
        return "claimed"
    if state == "finished" and outcome in REOPENABLE:
        return "reopen"
    if state == "finished" and outcome == "approved":
        return "approved"
    return "other"


def reason(by: str, why: str) -> str:
    """The row's reason after the reopen: who reopened it and why, on one line."""
    return f"reopened by {by}: {' '.join(why.split())}"


def change(row: Row, by: str, why: str) -> dict[str, object]:
    """The `reopen` change (`store_phases.apply_changes`) for a row `verdict` reopens: its own requirements and place
    kept, at the epoch it was read at, so a claim since would make it match nothing."""
    raw = row.get("requires_json")
    requires = [str(r) for r in cast("Sequence[object]", raw)] if isinstance(raw, list) else []
    return {"op": "reopen", "initiative": row.get("initiative"), "phase": row.get("phase"), "requires": requires,
            "band": row.get("band", 1), "seq": row.get("seq", 0), "epoch": row.get("claim_epoch", 0),
            "reason": reason(by, why)}


def note(initiative: str, phase: str, found: Verdict, prior: Row | None, after: Row | None) -> str:
    """What `cox worker reopen` says it did, or why it did nothing."""
    key = f"{initiative}/{phase}"
    was = prior or {}
    if found == "reopen":
        now = after or {}
        return (f"worker: {key} was finished {was.get('outcome')} (epoch {was.get('claim_epoch')}, "
                f"{was.get('attempts')} attempt(s): {was.get('reason') or 'no reason recorded'}); it is "
                f"{now.get('state')} now with {now.get('attempts')} attempts, \"{now.get('reason')}\", and a new review "
                "episode; the next claim resumes from the tasks already on the phase branch")
    return {
        "already": f"worker: {key} is ready already ({was.get('attempts')} attempt(s)); nothing changed",
        "absent": f"worker: {key} has no phase queue row: it is not a phase on the new model; nothing changed",
        "claimed": f"worker: {key} is claimed by {was.get('claimed_by')} (epoch {was.get('claim_epoch')}); nothing "
                   "changed",
        "approved": f"worker: {key} finished approved; there is nothing to retry; nothing changed",
        "other": f"worker: {key} is {was.get('state')}{' ' + str(was['outcome']) if was.get('outcome') else ''}, "
                 "not finished failed or quarantined; the chair moves it; nothing changed",
    }[found]


def release_hints(initiative: str, left: Sequence[tuple[str, str]]) -> list[str]:
    """What `cox route release` adds for each phase (phase, outcome) of `initiative` it leaves finished failed or
    quarantined: releasing a hold does not retry a finished phase."""
    return [f"release: phase {initiative}/{phase} is finished {outcome}; reopen it with "
            f"`cox worker reopen {initiative}/{phase} --reason ...`" for phase, outcome in left]
