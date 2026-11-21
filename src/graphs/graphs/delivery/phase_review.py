"""phase-review — one review round of a whole phase: its branch read against its base, with the phase smoke as evidence.

    review_phase -> review_adversary -> [arbitrate] -> emit

The phase lane (L6) builds a phase's tasks without a review each: a task is planned, built, checked and rolled up
onto the phase branch, and the phase is reviewed once, after its smoke has passed at the branch's tip. This graph is
that review. Its reviewer, adversary and arbiter are lifecycle-propose's own (`_review_round`), under the same tier
arithmetic and arbitration trigger, lifted from one task's patch to the branch's diff against its base. The smoke's
checks fill the slot a build's `commands_run` fills there, so the reviewers meet them under the sentence that says
the harness ran them.

The reviewer answers two questions of the phase as a whole (the `review-phase` skill): does it meet the phase's goal
(`validate_phase`'s goal check, folded into the prompt), and does it carry fat. Its verdict is the phase's:

- `approve`: the goal is met and nothing needs to go;
- `revise`: one revision of the branch fixes what the findings name (the lane builds it, `review_loop.revision_task`);
- `reject`: what is wrong is more than one revision can fix, and the phase is quarantined with the reason.

The first reviewer's seat is `review_phase` when the team binds it, else `review_charter`. A phase is reviewed at
tier 1 or above: the adversary runs whenever the team binds one, whatever the diff's size. A round where both
reviewers abstained, or a node failed, decides nothing: the result says `harness_fault` and the lane writes no round.

Advisory like every graph: no proposals, and nothing is written. The lane records the round.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from graphs._contract import require, require_cartridge, review_tier
from graphs._spec import GraphSpec, Need
from graphs.delivery.lifecycle_propose import _change_facts, _file_chunks, _NodeFailure, _review_round, patch_parses
from runner.protocol import NodeRunner

__all__ = ["GRAPH_NAME", "SPEC", "run"]

GRAPH_NAME = "phase-review"
PHASE_ROLE = "review_phase"
_PHASE_VERDICTS = {"approve": "approve", "revise": "revise"}  # anything else quarantines the phase

QUESTIONS = (
    "Review the whole phase, not one task: the diff below is its branch against its base, every task and revision "
    "together. Answer two questions, and name the file that proves each answer.\n"
    "1. Does the phase meet its goal? Judge against the goal, not the task list: tasks that each passed their own "
    "checks can still not add up, and 'every task finished' is not the goal being met.\n"
    "2. Does it carry fat? Code the goal does not need: a helper written twice, a function or module nothing the "
    "phase reaches calls, a layer or parameter that only passes a value through, a test of nothing the phase "
    "changed. Each is a finding with its file.\n"
    "approve: the goal is met and there is no fat. revise: one revision of the branch fixes every finding; say "
    "exactly what to change, file by file, since the findings are the revision task. reject: the goal is missed in a "
    "way one revision cannot fix; the rationale is the reason the phase is quarantined."
)


def _smoke_rows(smoke: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The smoke's checks as harness_verify evidence rows: the command and how it came out."""
    checks = [c for c in smoke.get("checks") or [] if isinstance(c, Mapping)]
    rows = [
        {"source": "harness_verify", "command": str(c.get("cmd") or c.get("name") or ""),
         "output": f"{'passed' if c.get('passed') is True else 'failed'} (exit {c.get('exit_code')})"
                   + (f": {' '.join(str(c.get('outcome')).split())[:300]}" if c.get("outcome") else "")}
        for c in checks
    ]
    verdict = str(smoke.get("verdict") or "")
    return rows or ([{"source": "harness_verify", "command": "phase smoke", "output": verdict}] if verdict else [])


def _ticket(args: Mapping[str, Any]) -> str:
    previous = [p for p in args.get("previous") or [] if isinstance(p, Mapping)]
    asked = "".join(f"\nRound {p.get('round')} said {p.get('verdict')}: {p.get('reason')}" for p in previous)
    return "\n".join([
        f"Phase {args['phase']} of initiative {args.get('initiative') or ''}, review round {args.get('round') or 1}.",
        f"THE PHASE'S GOAL, which is what you are judging against:\n{args.get('goal') or ''}",
        *([f"Its tasks: {', '.join(str(t) for t in args.get('tasks') or [])}"] if args.get("tasks") else []),
        *([f"What earlier rounds of this phase said (their revisions are in the diff):{asked}"] if asked else []),
        QUESTIONS,
    ])


def _reason(verdict: str, review: Mapping[str, Any], adversary: Mapping[str, Any] | None,
            arbitration: Mapping[str, Any] | str | None) -> str:
    """The round's reason: the arbiter's when it decided, else the reviewer's rationale (and the adversary's
    strongest objection when it did not approve)."""
    if isinstance(arbitration, Mapping) and arbitration.get("reasoning"):
        return " ".join(str(arbitration["reasoning"]).split())
    parts = [str(review.get("rationale") or "")]
    if adversary and adversary.get("verdict") != "approve":
        parts.append(str(adversary.get("strongest_objection") or ""))
    text = "; ".join(" ".join(p.split()) for p in parts if p.strip())
    return text or f"the review said {verdict}"


def _result(args: Mapping[str, Any], verdict: str, reason: str, *, tier: int | None = None,
            review: Mapping[str, Any] | None = None, adversary: Mapping[str, Any] | None = None,
            arbitration: Mapping[str, Any] | str | None = None, fault: str = "",
            facts: Mapping[str, Any] | None = None, role: str = "") -> dict[str, Any]:
    return {
        "run_id": args["run_id"], "date": args["date"], "initiative": args.get("initiative") or "",
        "phase": args["phase"], "round": int(args.get("round") or 1), "goal": args.get("goal") or "",
        "verdict": verdict, "reason": reason, "review_tier": tier, "reviewer": role,
        "review": dict(review or {}), "adversary": adversary, "arbitration": arbitration,
        "change_facts": dict(facts or {}), "harness_fault": fault,
    }


def run(args: Mapping[str, Any], runner: NodeRunner) -> dict[str, Any]:
    """Run the graph: one review round of the phase branch's diff against its base."""
    cartridge = require_cartridge(args)
    _, _, _, patch = require(args, "run_id", "date", "phase", "patch")
    bound = cartridge.get("skills") or {}
    role = PHASE_ROLE if PHASE_ROLE in bound else "review_charter"
    rows = _smoke_rows(args.get("smoke") or {})
    build = {
        "patch": patch, "summary": "the phase branch against its base: every task's roll-up and every revision",
        "files_touched": [path for path, _ in _file_chunks(patch)], "commands_run": rows, "reused": True,
    }
    facts = _change_facts(build)
    unusable = patch_parses(patch) if patch.strip() else "the phase branch holds no change against its base"
    if unusable is not None:
        return _result(args, "quarantine", unusable, facts=facts, role=role)
    surfaces: Sequence[str] = list(args.get("surfaces") or [])
    tier = max(review_tier(cartridge, change_facts=facts, surfaces=surfaces, patterns=list(args.get("patterns") or [])), 1)
    try:
        review, adversary, arbitration, verdict, _, abstained = _review_round(
            runner, context=list(cartridge.get("context") or []), bound=bound, ticket=_ticket(args), build=build,
            facts=facts, handoff=None, tier=tier, attempt=int(args.get("round") or 1),
            task_id=f"{args['phase']}-review-{int(args.get('round') or 1)}", charter_role=role,
        )
    except _NodeFailure as exc:
        return _result(args, "", "", tier=tier, review=exc.review, adversary=exc.adversary, fault=str(exc),
                       facts=facts, role=role)
    if abstained:
        return _result(args, "", "", tier=tier, review=review, adversary=adversary, facts=facts, role=role,
                       fault="both reviewers returned a placeholder twice and produced no judgment")
    phase_verdict = _PHASE_VERDICTS.get(verdict, "quarantine")
    return _result(args, phase_verdict, _reason(verdict, review, adversary, arbitration), tier=tier, review=review,
                   adversary=adversary, arbitration=arbitration, facts=facts, role=role)


SPEC = GraphSpec(
    name="phase-review",
    graph_name=GRAPH_NAME,
    run=run,
    summary="review a phase branch against its base, with the phase smoke as evidence: approve, revise or quarantine",
    needs=(
        Need("phase", flag="--phase", help="the phase under review"),
        Need("goal", flag="--goal", kind="text_or_path", required=False, help="the phase's goal, or a path to it"),
        Need("patch", flag="--patch", kind="text_or_path", help="the phase branch's diff against its base, or a path"),
        Need("smoke", flag="--smoke", kind="json_file", required=False,
             help="the phase smoke's result at the tip ({verdict, checks})"),
    ),
)
