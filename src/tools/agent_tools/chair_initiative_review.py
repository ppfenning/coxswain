"""The chair's side of the initiative review (D2), pure: when a new-model initiative's review is queued, and what a
`revise` round has the chair write.

A new-model initiative whose landing record reads `assembled` (every phase merged into its initiative branch, and no
pull request yet) is reviewed as a whole before that pull request opens (`harness.initiative_review`). For each, the
chair plans:

- `insert`: its one `initiative_review` row, under the reserved phase `initiative_review.QUEUE_PHASE`, once, with what
  its phases' rows require of a worker (the repository, and docker while the lane-prerequisite gate is on);
- `revise_initiative`, once the row finished `revised`: the revision phase the round asked for (`revise-<round>`), as
  one task row (the reviewers' asks, from the round's revision) and its `build` queue row, cut from the merged
  initiative branch, both written in one transaction (`store_api.phase_changes_on`);
- `rereview`, once that revision phase has merged and the initiative is assembled again: the same row claimable
  again, in the same episode, so the next round counts towards the episode's cap and never starts it afresh;
- `reopen` of a row withdrawn while its initiative was held, once nothing holds it (no episode ends).

An approved, quarantined or failed review is the lander's to act on (`lander_review.pr_gate`), and nothing here
reopens it. A ready review row whose initiative becomes held is withdrawn by `chair_phase_queue.changes`, as any row is.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from typing import cast

from core import landing_record as lr
from harness import initiative_review, review_loop

Row = Mapping[str, object]

__all__ = ["review_changes", "revision_change"]


def _tasks_of(tasks: Sequence[Row], initiative: str) -> list[dict[str, object]]:
    """The initiative's task rows (`store_phases.task_commits`) as the landing record reads tasks: id, phase, state."""
    return [{"id": str(t.get("task_id") or t.get("id") or ""), "phase": str(t.get("phase") or ""),
             "state": str(t.get("state") or "")}
            for t in tasks if str(t.get("initiative")) == initiative]


def _place(stored: Sequence[Row], initiative: str) -> tuple[str, int]:
    """The repository the initiative's phase rows name, and the queue order just after its last phase."""
    own = [r for r in stored if str(r.get("initiative")) == initiative and r.get("kind") != initiative_review.KIND]
    repo = next((str(r.get("repo") or "") for r in own if r.get("repo")), "")
    seq = max((int(str(r.get("seq") or 0)) for r in own), default=0) + 1
    return repo, seq


def revision_change(initiative: str, number: int, revision: Mapping[str, object], branch: str, repo: str,
                    requires: Sequence[str], seq: int) -> dict[str, object]:
    """Pure. The `revise_initiative` change for initiative review round `number`: the revision phase's one task row
    (id and phase `initiative_review.revision_phase(number)`, the round's revision task's title, body and surfaces)
    and that phase's `build` queue row, cut from `branch`, the merged initiative branch."""
    phase = initiative_review.revision_phase(number)
    surfaces = revision.get("surfaces")
    return {
        "op": initiative_review.REVISE_OP, "initiative": initiative, "phase": phase,
        "task": {"task_id": phase, "kind": "task", "phase": phase, "state": "ready", "needs": [],
                 "title": str(revision.get("title") or f"Revise {initiative} after initiative review round {number}"),
                 "surfaces": [str(s) for s in cast("list[object]", surfaces)] if isinstance(surfaces, list) else [],
                 "body": str(revision.get("body") or f"Initiative review round {number} asked for one revision."),
                 "extra": {"revision_of": {"review": initiative_review.KIND, "round": number}}},
        "row": {"initiative": initiative, "phase": phase, "kind": "build", "repo": repo, "base_ref": branch,
                "requires": list(requires), "band": 1, "seq": seq,
                "reason": f"initiative review round {number} asked for this revision"},
    }


def review_changes(records: Sequence[Mapping[str, object]], tasks: Sequence[Row], stored: Sequence[Row],
                   rounds: Mapping[str, Sequence[Mapping[str, object]]],
                   requires: Callable[[str], list[str]], withheld: Collection[str] = ()) -> list[dict[str, object]]:
    """Pure. The phase-queue changes the initiative reviews need, as the module docstring says. `records` are the
    landing records of the tick's phase initiatives, `tasks` their task rows, `stored` the phase queue, `rounds` each
    initiative's own review rounds (phase '', every episode's), `requires` what a row of an initiative's repository
    requires (`chair_phase_queue.requirements`); an initiative in `withheld`, or whose record is held, gets nothing."""
    rows = {str(r.get("initiative")): r for r in stored if str(r.get("phase")) == initiative_review.QUEUE_PHASE}
    out: list[dict[str, object]] = []
    for found in sorted(records, key=lambda r: str(r.get("initiative") or "")):
        record = cast("lr.LandingRecord", found)
        initiative = str(record.get("initiative") or "")
        if lr.record_lane_model(record) != lr.NEW_MODEL or initiative in withheld or record.get("held"):
            continue
        state = lr.initiative_state(record, _tasks_of(tasks, initiative))
        row, (repo, seq) = rows.get(initiative), _place(stored, initiative)
        held = review_loop.episode(review_loop.rounds_of(rounds.get(initiative) or ()))
        last = held[-1] if held else None
        branch = str(record.get("branch") or "")
        if row is None:
            if state == "assembled":
                out.append({"op": "insert", "initiative": initiative, "phase": initiative_review.QUEUE_PHASE,
                            "kind": initiative_review.KIND, "repo": repo, "base_ref": branch,
                            "requires": requires(repo), "band": 1, "seq": seq,
                            "reason": f"{initiative} is assembled: every phase merged into {branch}"})
        elif row.get("state") == "finished" and row.get("outcome") == "revised" and last is not None \
                and last.verdict == "revise":
            phase = initiative_review.revision_phase(last.number)
            if not any(t["phase"] == phase for t in _tasks_of(tasks, initiative)):
                out.append(revision_change(initiative, last.number, last.revision, branch, repo, requires(repo), seq))
            elif state == "assembled":
                out.append({"op": initiative_review.REREVIEW_OP, "initiative": initiative,
                            "phase": initiative_review.QUEUE_PHASE, "epoch": row.get("claim_epoch", 0),
                            "reason": f"{phase} merged into {branch}: round {last.number + 1} reviews it"})
        elif row.get("state") == "cancelled" and state == "assembled":
            out.append({"op": "reopen", "initiative": initiative, "phase": initiative_review.QUEUE_PHASE,
                        "epoch": row.get("claim_epoch", 0), "requires": requires(repo), "band": 1, "seq": seq,
                        "reason": "nothing holds its initiative any more"})
    return out
