"""The phase review loop's rules, pure (L6): what a phase lane does next once its tasks have rolled up, and the one
revision task a `revise` round asks for.

The lane runs, in order: the phase smoke at the tip (L2's `_smoke`), the review (`phase-review`), and on `revise` the
revision task, built and rolled up like any task, then the smoke again at the new tip and the next round, until a round
approves or quarantines the phase. `next` reads where that stands from three facts and gives the one next step:

- the phase's review rounds from the store (`review_rounds`), oldest first. Only the current **episode** counts: when
  the chair reopens a finished phase (work came back to it) it writes a `reopened` row (`REOPENED`), and every round
  up to it belongs to an episode that is over. So a reopened phase never finishes on the `approve` of the last one;
- what git shows (`Progress.revisions`: the rounds whose revision commit is on the phase branch) and what the lane has
  seen pass (`Progress.smoked`: the rounds whose smoke passed at the branch's current tip);
- the revision budget: at most `cap` review rounds an episode, and, when the phase has a budget (`budget` > 0), the
  money the episode's rounds and revisions have spent (`spent`).

Round numbers keep counting across episodes (the store keys a round by its number), so an episode's first round is
the one after the last row, whatever it was.

A revise whose revision the budget cannot buy is not a revision: the lane records that round as a quarantine with
`BUDGET_SPENT` as its reason (`recorded`), so the store holds the verdict that finished the phase and the worker
reads it as every other round's.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, cast

__all__ = [
    "BUDGET_SPENT",
    "CAP",
    "QUARANTINE_TASK",
    "REOPENED",
    "REVIEW_CAUSE",
    "Progress",
    "Round",
    "Step",
    "episode",
    "next",
    "recorded",
    "revision_id",
    "revision_task",
    "rounds_of",
]

CAP = 3  # review rounds an episode may hold
REOPENED = "reopened"  # the chair's row that closes an episode when it reopens the phase
BUDGET_SPENT = "revision budget spent"
# The chair's reaction to a quarantined phase: one `queue_phases` change per task (`store_api.phase_changes_on`), whose
# quarantine carries the cause triage reads, the review's.
QUARANTINE_TASK = "quarantine_task"
REVIEW_CAUSE = "review"
_FINISHED = {"approve": "approved", "quarantine": "quarantined"}

Action = Literal["smoke", "review", "build_revision", "finish"]


@dataclass(frozen=True)
class Round:
    """One `review_rounds` row: its number, verdict (`approve`, `revise`, `quarantine`, or `REOPENED`), why, the
    revision task a revise asked for (`revision_task`, empty otherwise), and what the round cost."""

    number: int
    verdict: str
    reason: str = ""
    revision: Mapping[str, object] = field(default_factory=dict[str, object])
    cost_usd: float = 0.0


@dataclass(frozen=True)
class Progress:
    """What the lane knows besides the rounds: the rounds whose revision commit git shows on the phase branch, and the
    rounds whose smoke passed at the branch's current tip."""

    revisions: frozenset[int] = frozenset()
    smoked: frozenset[int] = frozenset()

    @classmethod
    def of(cls, revisions: Collection[int], smoked: Collection[int]) -> Progress:
        return cls(frozenset(revisions), frozenset(smoked))


@dataclass(frozen=True)
class Step:
    """The lane's next step. `smoke` and `review` name the round they are for; `build_revision` the round whose
    revision task to build; `finish` the outcome (`approved` or `quarantined`) and, for a quarantine, why."""

    action: Action
    round: int = 0
    outcome: str = ""
    reason: str = ""


def _number(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else int(str(value or 0))


def _money(value: object) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def rounds_of(rows: Sequence[Mapping[str, object]]) -> tuple[Round, ...]:
    """The store's `review_rounds` rows ({round, verdict, reason, revision_json, cost_usd}) as `Round`s, by number."""
    out = [
        Round(_number(r.get("round")), str(r.get("verdict") or ""), str(r.get("reason") or ""),
              cast("Mapping[str, object]", r.get("revision_json")) if isinstance(r.get("revision_json"), Mapping) else {},
              _money(r.get("cost_usd")))
        for r in rows
    ]
    return tuple(sorted(out, key=lambda r: r.number))


def episode(rounds: Sequence[Round]) -> tuple[Round, ...]:
    """The review rounds of the current episode: every round after the last `REOPENED` row, oldest first."""
    ordered = sorted(rounds, key=lambda r: r.number)
    opened = max((i for i, r in enumerate(ordered) if r.verdict == REOPENED), default=-1)
    return tuple(r for r in ordered[opened + 1:] if r.verdict != REOPENED)


def _afford(held: int, spent: float, cap: int, budget: float) -> bool:
    """Whether one more revision, and the round after it, fit: fewer than `cap` rounds held and money left."""
    return held < cap and not (budget > 0 and spent >= budget)


def next(rounds: Sequence[Round], progress: Progress, cap: int = CAP, spent: float = 0.0, budget: float = 0.0) -> Step:
    """The lane's next step once every task of the phase has rolled up.

    - The episode's last round approved or quarantined: `finish` with that outcome (a quarantine with its reason).
    - It said revise: once git holds its revision, the next round (`smoke` first, until the smoke passed at the new
      tip, then `review`); before, `build_revision`, while the budget still buys one; once it does not, `finish`
      quarantined, `BUDGET_SPENT`.
    - No round yet this episode: round one past the last row (`smoke`, then `review`)."""
    held = episode(rounds)
    last = held[-1] if held else None
    if last is not None and last.verdict in _FINISHED:
        return Step("finish", last.number, _FINISHED[last.verdict], last.reason if last.verdict == "quarantine" else "")
    if last is not None and last.verdict == "revise" and last.number not in progress.revisions:
        if not _afford(len(held), spent, cap, budget):
            return Step("finish", last.number, "quarantined", BUDGET_SPENT)
        return Step("build_revision", last.number)
    upcoming = max((r.number for r in rounds), default=0) + 1
    return Step("review" if upcoming in progress.smoked else "smoke", upcoming)


def recorded(rounds: Sequence[Round], number: int, verdict: str, reason: str, progress: Progress, cap: int = CAP,
             spent: float = 0.0, budget: float = 0.0) -> tuple[str, str]:
    """The (verdict, reason) the lane writes for round `number`, whose review said `verdict`: as it said, except a
    revise the budget cannot buy (`next` would finish it), which is written as the quarantine it comes to."""
    if verdict != "revise":
        return verdict, reason
    step = next([*rounds, Round(number, "revise", reason)], progress, cap, spent, budget)
    return ("quarantine", f"{BUDGET_SPENT}: {reason}" if reason else BUDGET_SPENT) if step.action == "finish" else (verdict, reason)


def revision_id(phase: str, number: int) -> str:
    """The revision task's id: `<phase>-rev-<round>`, as its step rows and roll-up trailers name it."""
    return f"{phase}-rev-{number}"


def _items(value: object) -> list[Mapping[str, object]]:
    return [cast("Mapping[str, object]", v) for v in cast("list[object]", value) if isinstance(v, Mapping)] \
        if isinstance(value, list) else []


def _part(output: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = output.get(key)
    return cast("Mapping[str, object]", value) if isinstance(value, Mapping) else {}


def _text(value: object) -> str:
    return " ".join(str(value or "").split())


def _findings(output: Mapping[str, object]) -> list[Mapping[str, object]]:
    """The reviewer's findings that say something."""
    return [f for f in _items(_part(output, "review").get("findings")) if _text(f.get("detail"))]


def _asks(output: Mapping[str, object]) -> list[str]:
    """What the reviewers asked to change, one line each: the reviewer's findings, then, when the arbiter sided with
    the adversary or no arbiter ran, the adversary's objections."""
    adversary = _part(output, "adversary")
    sided = _text(_part(output, "arbitration").get("sided_with"))
    lines = [f"{_text(f.get('file')) or '(no file)'}: {_text(f.get('detail'))}"
             + (f" [{_text(f.get('charter_principle'))}]" if _text(f.get("charter_principle")) else "")
             for f in _findings(output)]
    if adversary.get("verdict") not in (None, "approve") and sided in ("", "adversary"):
        lines += [f"{_text(o.get('claim'))}: {_text(o.get('why_wrong'))}"
                  for o in _items(adversary.get("objections")) if _text(o.get("why_wrong"))]
    return lines


def _files(output: Mapping[str, object]) -> list[str]:
    return sorted({_text(f.get("file")) for f in _findings(output) if _text(f.get("file"))})


def revision_task(number: int, reviewer_output: Mapping[str, object]) -> dict[str, object]:
    """The one revision task review round `number` asks for, from the `phase-review` graph's result (its `phase`,
    `goal`, `reason`, and the reviewers' answers): a work item the lane builds like any task of the phase, whose
    roll-up is the round's revision commit (`Cox-Kind: revision`, `Cox-Round`). Its body carries what the reviewers
    asked for, line by line, and the phase's goal, so the builder fixes the phase, not one task."""
    phase = str(reviewer_output.get("phase") or "")
    goal = _text(reviewer_output.get("goal"))
    asks = _asks(reviewer_output) or [_text(reviewer_output.get("reason")) or "see the review"]
    body = "\n".join([
        f"Review round {number} of phase {phase} asked for one revision of the phase branch.",
        *([f"\nThe phase's goal: {goal}"] if goal else []),
        "\nChange exactly this, and nothing the reviewers did not ask for:",
        *(f"- {line}" for line in asks),
    ])
    return {
        "id": revision_id(phase, number), "title": f"Revise {phase} after review round {number}", "phase": phase,
        "state": "ready", "kind": "revision", "round": number, "needs": [], "surfaces": _files(reviewer_output),
        "body": body,
    }
