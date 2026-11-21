"""The initiative review loop's rules, pure (D2): an assembled new-model initiative is reviewed as a whole before its one
pull request, and the full suite gates that pull request.

Once every phase of a new-model initiative has merged into its initiative branch (its landing record `assembled`),
the chair queues one `initiative_review` row (`KIND`) in the phase queue, under the reserved phase `QUEUE_PHASE`, as
`integrate` is reserved for the old model. A worker claims it like any phase, and its lane runs the review loop L6
runs for a phase (`review_loop.next`, the same function), on the initiative instead: its rounds and its step rows are
the initiative's own, under phase `''` (`ROUNDS_PHASE`, which `review_rounds` and `step_results` keep for it), and
only the current episode's rounds count. Three things differ from a phase:

- no smoke before a round: every phase passed its own before it merged, and the gate after the review is the suite;
- a `revise` round's revision is a phase of its own (`revision_phase`: `revise-<round>`), which the chair writes as
  one task row and its `build` queue row, cut from the merged initiative branch. The lane ends on the revise (the
  worker finishes the row `revised`); the revision phase is built, reviewed and approved like any phase and merged by
  the lander; once the initiative is assembled again the chair queues the review again, in the same episode, and the
  lane reviews the next round (`merged_revisions`). Its own phase id keeps L5's trailer adoption from reading its
  commits as an earlier phase's;
- on `approve`, the `ticket` gate (the cartridge's `ticket_checks` and `.agent-checks`' `# level: initiative`
  section, the full suite) runs once at the tip, and one `suite` step row (`phase/suite/<round>`) records it. The
  lander opens the pull request only when the episode's last round approved and a passing `suite` row stands at the
  initiative branch's current tip (`lander_review.pr_gate`).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from harness import phase_pick, review_loop

__all__ = [
    "KIND",
    "QUEUE_PHASE",
    "REREVIEW_OP",
    "REVISE_OP",
    "ROUNDS_PHASE",
    "SUITE_FAILED",
    "SuiteSeen",
    "ended",
    "initiative_goal",
    "lane_verdict",
    "merged_revisions",
    "next_step",
    "revision_phase",
    "revision_task",
    "rounds_phase",
    "suite_at",
]

KIND = "initiative_review"  # the phase queue row's kind
QUEUE_PHASE = "initiative"  # the row's phase in the phase queue: reserved, as the old model's `integrate` is
ROUNDS_PHASE = ""  # the phase an initiative's review rounds and step rows are written under
SUITE_FAILED = "the suite failed: "  # the prefix of a failed suite row's reason
# The chair's `queue_phases` changes for the review (`chair_initiative_review`): a revision phase's task row and its
# build queue row, written together (`store_api.phase_changes_on`); and a `revised` review made claimable again in the
# same episode once that phase has merged (`store_phases._apply`).
REVISE_OP = "revise_initiative"
REREVIEW_OP = "rereview"


def rounds_phase(queue_phase: str) -> str:
    """Pure. The phase a queue row's review rounds and phase step rows are under: `''` for the initiative review's
    row, else the row's own phase."""
    return ROUNDS_PHASE if queue_phase == QUEUE_PHASE else queue_phase


def initiative_goal(initiative: Mapping[str, object]) -> str:
    """Pure. What the initiative review judges against: the initiative's own title and prose, as `epic_context.
    phase_goal` gives a phase with no recorded goal its initiative's, never the task list."""
    title = str(initiative.get("title") or "").strip()
    body = str(initiative.get("body") or "").strip()
    return f"initiative '{initiative.get('id')}'" + (f": {title}" if title else "") + (f"\n\n{body}" if body else "")


def revision_phase(number: int) -> str:
    """Pure. The revision phase initiative review round `number` asks for, `revise-<round>`; its one task has the same id."""
    return f"revise-{number}"


def revision_task(initiative: str, number: int, reviewer_output: Mapping[str, object]) -> dict[str, object]:
    """Pure. The revision a `revise` round of the initiative asks for, from the `phase-review` graph's result: L6's
    revision task (`review_loop.revision_task`: what the reviewers asked for, line by line, and the goal) as the one
    task of its own phase, `revision_phase(number)`, which the chair writes and a lane builds."""
    task = review_loop.revision_task(number, reviewer_output)
    lines = str(task.get("body") or "").splitlines()
    head = f"Initiative review round {number} of {initiative} asked for one revision of the initiative branch."
    phase = revision_phase(number)
    return {**task, "id": phase, "phase": phase, "title": f"Revise {initiative} after initiative review round {number}",
            "body": "\n".join([head, *lines[1:]])}


def merged_revisions(rounds: Sequence[review_loop.Round], merged_phases: Collection[str]) -> frozenset[int]:
    """Pure. The episode's revise rounds whose revision phase has merged into the initiative branch: what git holds
    of them, as `review_loop.Progress.revisions` reads a phase's revision commits."""
    return frozenset(r.number for r in review_loop.episode(rounds)
                     if r.verdict == "revise" and revision_phase(r.number) in merged_phases)


def next_step(rounds: Sequence[review_loop.Round], merged_phases: Collection[str], spent: float = 0.0,
              budget: float = 0.0) -> tuple[review_loop.Step, review_loop.Progress]:
    """Pure. The initiative lane's next step, `review_loop.next`'s with no smoke before a round (the next round counts
    as smoked), and the progress it was read from: `review` (the next round), `build_revision` (a revise whose
    revision phase has not merged: the lane ends, and the chair writes it), or `finish`."""
    upcoming = max((r.number for r in rounds), default=0) + 1
    progress = review_loop.Progress.of(merged_revisions(rounds, merged_phases), {upcoming})
    return review_loop.next(rounds, progress, spent=spent, budget=budget), progress


@dataclass(frozen=True)
class SuiteSeen:
    """One `suite` step row as the worker and the lander read it: whether it passed, the tip it ran at, why not, and
    the run that ran it."""

    ok: bool
    tip: str
    reason: str = ""
    run_id: str = ""


def suite_at(rows: Sequence[Mapping[str, object]], tip: str) -> SuiteSeen | None:
    """Pure. The last `suite` row at `tip` of `rows` (oldest first, as `store_phases.suite_rows` reads them), or None
    when none ran there."""
    if not tip:
        return None
    for row in reversed(rows):
        if row.get("subject_sha") == tip:
            result = row.get("result_json")
            reason = str(result.get("reason") or "") if isinstance(result, Mapping) else ""
            return SuiteSeen(row.get("ok") in (1, True), tip, reason, str(row.get("run_id") or ""))
    return None


def ended(round_rows: Sequence[Mapping[str, object]],
          suite_rows: Sequence[Mapping[str, object]]) -> tuple[tuple[int, str] | None, str, SuiteSeen | None]:
    """Pure. What the worker reads of an initiative review once its lane ended, from the initiative's `review_rounds`
    rows and its `suite` rows: the current episode's last round as (round, verdict), that round's reason, and the
    suite at the tip that round reviewed (None when none ran there)."""
    held = review_loop.episode(review_loop.rounds_of(round_rows))
    if not held:
        return None, "", None
    last = held[-1]
    head = next((str(r.get("head_sha") or "") for r in round_rows if str(r.get("round")) == str(last.number)), "")
    return (last.number, last.verdict), last.reason, suite_at(suite_rows, head)


def lane_verdict(last_round: tuple[int, str] | None, suite: SuiteSeen | None, attempts: int,
                 max_attempts: int = phase_pick.MAX_ATTEMPTS, reason: str = "") -> phase_pick.Verdict:
    """Pure. The worker's ack or nack for an initiative review whose lane ended, from the episode's last round (and
    its `reason`) and the suite at the tip that round reviewed:

    - approve, and the suite passed: `approved`; the lander opens the pull request;
    - approve, and the suite failed: `failed`, at once (a retry runs the same suite at the same tip); the lander holds
      the initiative, and new work or a person moves it;
    - quarantine: `quarantined`, with the round's reason; the lander holds;
    - revise: `revised`; the chair writes the revision phase, and queues the review again once it has merged;
    - anything else (no round yet, or an approval with no suite: the lane ended early) is retried with backoff, and
      after `max_attempts` claims the review has failed."""
    if last_round is not None and last_round[1] == "approve" and suite is not None:
        if suite.ok:
            return phase_pick.Verdict("ack", "approved", f"the initiative review approved it in round {last_round[0]} "
                                                         f"and the suite passed at {suite.tip[:12]}")
        return phase_pick.Verdict("quarantine", "failed", f"the suite failed at {suite.tip[:12]}: "
                                                          f"{suite.reason.removeprefix(SUITE_FAILED)}")
    if last_round is not None and last_round[1] == "quarantine":
        return phase_pick.Verdict("quarantine", "quarantined", f"the initiative review quarantined it in round "
                                                               f"{last_round[0]}" + (f": {reason}" if reason else ""))
    if last_round is not None and last_round[1] == "revise":
        return phase_pick.Verdict("ack", "revised", f"the initiative review's round {last_round[0]} asked for "
                                                    f"{revision_phase(last_round[0])}" + (f": {reason}" if reason else ""))
    why = ("the review approved it but the lane ended before the suite ran" if last_round is not None
           else "the lane ended before a review round was written")
    if attempts >= max_attempts:
        return phase_pick.Verdict("quarantine", "failed", f"{attempts} attempt(s), the last: {why}")
    return phase_pick.Verdict("retry", "", why, phase_pick.backoff_s(attempts))
