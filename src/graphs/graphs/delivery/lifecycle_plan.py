"""lifecycle-propose's planning: the competition between two plans and the attack on the winner.

See the graph's own docstring (`lifecycle_propose`) for why solutions compete before anyone builds.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NamedTuple

from graphs._contract import (
    ContractViolation,
)
from graphs.delivery.lifecycle_schemas import PLAN_ATTACK_SCHEMA, PLAN_CHOICE_SCHEMA, PLAN_SCHEMA
from runner.protocol import NodeRunner
from runner.tier_resolution import Hints


class _Author(NamedTuple):
    """Who wrote a plan: the role, its tier, and the thread it was written on.

    A revision has to go back to the seat that holds the plan's context. The
    first planner keeps the ticket's thread, which build later joins; the
    second planner and the arbiter each keep a thread of their own, so that a
    revision can continue where they left off without ever joining the first
    planner's — independence is the whole value of a second plan.
    """

    role: str
    tier: str
    thread: str


def _plan_competition(
    runner: NodeRunner,
    *,
    context: list[str],
    ticket: Any,
    date: Any,
    plan: Mapping[str, Any],
    first: _Author,
    task_id: Any,
) -> tuple[dict[str, Any], dict[str, Any], _Author]:
    """A second, independent plan, and a decision between the two.

    The second planner is shown the first plan only so it can avoid repeating
    it — it is told to differ, not to critique — and it never joins the first
    planner's thread, for the same reason review never joins the builder's.
    The arbiter picks, or merges. When it picks, the builder gets the source
    plan VERBATIM, not the arbiter's restatement of it: what was compared is
    what gets built. Only a merge is the arbiter's own plan.

    Returns the winning plan, the record, and the winner's AUTHOR — the seat
    a revision goes back to. A pick's author is the planner that wrote it; a
    merge's author is the arbiter, because neither planner wrote that plan.
    """
    second = _Author("plan_alternative", "standard", f"{first.thread}/plan_alternative")
    arbiter = _Author("plan_arbitrate", "deep", f"{first.thread}/plan_arbitrate")
    alternative = dict(
        runner.run(
            role=second.role,
            tier=second.tier,
            thread=second.thread,
            task=str(task_id),
            schema=PLAN_SCHEMA,
            context=context,
            prompt=(
                "A first plan for this ticket already exists. Write a second one "
                "that takes a materially different route — a different "
                "decomposition, different files, or a different order — so that "
                "a comparison is worth making. Do not critique the first plan; "
                "produce a whole plan of your own.\n\n"
                f"Ticket: {ticket}\nDate: {date}\n\n"
                f"First plan (do not repeat it): {plan}\n\n"
                "Name the files you expect to touch, and state what is explicitly "
                "out of scope."
            ),
        )
    )
    choice = dict(
        runner.run(
            role=arbiter.role,
            tier=arbiter.tier,
            thread=arbiter.thread,
            task=str(task_id),
            schema=PLAN_CHOICE_SCHEMA,
            context=context,
            prompt=(
                "Two independent plans exist for this ticket. Choose the one the "
                "builder should carry out, or merge them into one that is better "
                "than either. Judge them against the ticket and the repository in "
                "front of you: steps that can be checked, files that exist, scope "
                "that stays bounded. Say what choosing it costs.\n\n"
                f"Ticket: {ticket}\n\nFirst plan: {plan}\n\nSecond plan: {alternative}"
            ),
        )
    )
    chosen = str(choice.get("chosen"))
    merged: Any = choice.get("plan")
    if chosen == "merged" and not merged:
        raise ContractViolation(
            f"plan_arbitrate claimed 'merged' for '{first.thread}' but returned no plan. "
            "A merge is the arbiter's own plan; the graph stops rather than "
            "building the first plan under that name — a revision would go "
            "back to the arbiter carrying a plan it never wrote."
        )
    winner, author = (
        (dict(plan), first) if chosen == "first"
        else (alternative, second) if chosen == "second"
        else (dict(merged), arbiter)
    )
    record = {
        "alternative": alternative,
        "chosen": chosen,
        "reasoning": str(choice.get("reasoning") or ""),
        "price": str(choice.get("price") or ""),
    }
    return winner, record, author


def _plan_tier(cartridge: Mapping[str, Any], *, surfaces: list[str], patterns: list[str]) -> int:
    """The tier a task reads as before anyone has built anything.

    Deliberately not a call into `review_tier`: a plan has no diff yet, so
    change_facts would have to be passed in empty, and `review_tier`'s size
    branches read an empty dict as zero changed lines in zero modules — which
    satisfies `tier0_max_changed_lines` and `tier1_max_changed_lines` for
    every task. A cartridge that sets `tier0_max_changed_lines` (one already
    does, for live epics) would then read every task without a tier2 surface
    as tier 0 before a line of code exists, silently disabling the
    competition under any floor above 0. This reads only the two policy keys
    a pre-build task can actually speak to — the dangerous surfaces and the
    trivial patterns — and never reaches a size branch at all.

    Note for the next reader: this graph never imports `core`, the same way
    it never imports `shell` or `harness` — only the harness imports the
    substrate, which is what lets CI collect this file with agent-cartridges
    absent.
    """
    config = (cartridge.get("policy") or {}).get("review_tier") or {}
    dangerous = set(config.get("tier2_surfaces") or [])
    if dangerous & set(surfaces):
        return 2
    trivial = set(config.get("tier0_patterns") or [])
    if patterns and set(patterns) <= trivial:
        return 0
    return 1


def _plan_gate(
    cartridge: Mapping[str, Any], bound: Mapping[str, Any], surfaces: list[str], patterns: list[str]
) -> tuple[int, int, bool, bool, bool]:
    """Whether the competition and the attack are worth their price on this task.

    Returns the tier, the configured floor, whether the competition PAIR is
    bound, whether the plan attacker is bound, and whether the floor is
    cleared. The two seats are reported separately on purpose: a cartridge that
    binds only the attacker never configured a competition, and must not be
    told one was skipped. The floor comparison is made exactly once, here.
    """
    tier = _plan_tier(cartridge, surfaces=surfaces, patterns=patterns)
    min_tier = int(((cartridge.get("policy") or {}).get("plan_competition") or {}).get("min_tier", 0))
    competition_bound = "plan_alternative" in bound and "plan_arbitrate" in bound
    attack_bound = "plan_adversary" in bound
    return tier, min_tier, competition_bound, attack_bound, tier >= min_tier


def _plan_attack(
    runner: NodeRunner,
    *,
    context: list[str],
    ticket: Any,
    author: _Author,
    plan: Mapping[str, Any],
    task_id: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The adversary, moved to the front of the loop.

    After `build`, an objection costs a rebuild. Before it, an objection costs
    one more plan. So the plan's claims — this file exists, this signature is
    what the test will call, this step can be checked without doing the next
    one — get attacked while they are still cheap to be wrong about.

    Bounded to ONE revision, by the plan's AUTHOR on the author's own thread:
    the first planner when its plan won or no competition ran, the second
    planner when the arbiter chose `second`, the arbiter itself when the plan
    is a merge. The objections travel verbatim. Sending a planner another
    planner's plan would hand it a thread already holding its own losing plan
    and call the result a revision. The revision is not attacked again: the
    review round after build is where the built plan gets judged, and a loop
    here would be a second fix loop with none of the first one's accounting.
    """
    attack = dict(
        runner.run(
            role="plan_adversary",
            task=str(task_id),
            hints=Hints(judgment="high"),
            schema=PLAN_ATTACK_SCHEMA,
            context=context,
            prompt=(
                "Your job is to disagree with this plan before anyone builds it. "
                "Attack the claims it rests on: a file or function it assumes "
                "exists, a signature it assumes, a step that cannot be checked, a "
                "step that depends on the next one, scope that has quietly grown. "
                "Check what you can against the repository.\n\n"
                f"Ticket: {ticket}\nPlan: {plan}\n\n"
                "State your strongest objection plainly, even if you conclude the "
                "plan can proceed."
            ),
        )
    )
    if attack.get("verdict") != "revise":
        return dict(plan), {"attack": attack, "revised": False}

    objections = "\n".join(
        f"- {objection.get('claim')} — {objection.get('why_wrong')}"
        for objection in attack.get("objections") or []
        if isinstance(objection, Mapping)
    )
    revised = dict(
        runner.run(
            role=author.role,
            tier=author.tier,
            thread=author.thread,
            task=str(task_id),
            schema=PLAN_SCHEMA,
            context=context,
            prompt=(
                "This plan was sent back before build. Revise it so that every "
                "objection below actually falls — a plan that leaves one standing "
                "is not a revision.\n\n"
                f"Ticket: {ticket}\nPlan: {plan}\n\n"
                f"Objections:\n{objections}\n"
                f"Strongest: {attack.get('strongest_objection')}\n\n"
                "Name the files you expect to touch, and state what is explicitly "
                "out of scope."
            ),
        )
    )
    return revised, {"attack": attack, "revised": True}
