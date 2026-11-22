"""lifecycle-propose — the development loop for ONE task.

    scope -> plan -> [plan_alternative -> plan_arbitrate] -> [plan_adversary]
          -> build (worktree) -> handoff -> review -> adversary -> arbitrate -> emit

Takes one task, produces reviewed work and proposals. Nothing is pushed, opened,
or merged. The build node returns a patch; applying it is the shell's job,
inside a worktree the shell owns.

**Solutions compete before anyone builds.** The reviewers after `build` judge
one diff; they can say ship, revise or reject, and nothing else. A different
design only ever appears if the one planner happens to think of it. So the
bracketed nodes let a team hold a competition at the cheap end of the loop: a
second planner writes an independent plan told to differ, an arbiter picks
one or merges them and names the price, and an adversary attacks the winning
plan's claims — files it assumes, steps that cannot be checked, scope that
grew — before a builder spends a budget on it. A plan costs a tenth of a
build, which is why the competition happens here and not between two diffs.
An objection the adversary sustains buys one revision, and the revision goes
back to whoever wrote the plan — the first planner on its thread, the second
on its own, or the arbiter when the plan is a merge of both. A planner is
never handed another planner's plan and told it is its own. Every one of
these roles is optional; unbound means absent. Even bound, the competition and
the attack run only when the tier read off the task's surfaces and patterns
reaches `policy.plan_competition.min_tier` — a docs-only task does not buy a
second planner just because a team happens to have one bound.

Three convictions shape the back half of this graph.

**Nothing is one-shot.** Every change gets a reviewer, and how many it gets is
proportional to what a mistake would cost — `review_tier` decides, not the
author. A dangerous surface earns an adversary and an arbitrator even when the
diff is four lines.

**A step never builds on an unvalidated handoff.** The `handoff` node checks
that what build produced actually satisfies what review needs before review sees
it, and REFUSES rather than passing a gap along. A phase that goes quietly wrong
usually did so three steps earlier.

**A fix loop must never launder struggle into trust.** Sending a rejected change
back to the builder is ordinary; forgetting that it was sent back is not. The
loop counts its attempts and carries the count out on the proposal, so a task
that passed on the third try stays distinguishable, everywhere downstream, from
one that passed clean. The ledger is what refuses to let a repeated-attempt pass
extend a streak — but it can only refuse what it can see, and this graph is the
only place that knows. A graph that quietly retried until something passed would
be manufacturing exactly the clean record the ledger exists to disbelieve.

Every node after `build` is an optional role: a team that binds none of them
gets the original single-reviewer loop, which is what optional means.

**Elevation.** A ticket's `tier:` map, `{role: tier}`, arrives as `args["tier"]`
and reaches every call this graph makes. A caller-named tier beats the profile
default in the runner's resolver. Where a call site still passes a literal tier,
the ticket's value wins only when it is higher: a ticket never lowers a literal.
The one automatic escalation is the second build after a review `revise`, which
asks one tier up and leaves a `tier escalation` evidence row. A ticket `tier:`
for `build` overrides it. A third attempt is never escalated: third attempts
landed nothing on 09-06.

Next step, not in this repository: per-launch elevation,
`cox route launch epic --elevate ROLE=TIER` (repeatable), lives in src/tools.

Deferred (see graphs/lifecycle-propose.md): intake queue, verification, retro.
"""

from __future__ import annotations

from collections.abc import Mapping
from difflib import SequenceMatcher
from typing import Any

from graphs._contract import (
    epic_shape,
    landing_for,
    proposal,
    require,
    require_cartridge,
    review_tier,
)
from graphs.delivery.clean_gate import clean_gate_outcome
from graphs.delivery.lifecycle_checkpoints import _Asking, _checkpoint_context, _Elevated, _escalate, _resume_build
from graphs.delivery.lifecycle_patch import _build_output_valid, _change_facts, _hints, _patch_sections, patch_parses
from graphs.delivery.lifecycle_plan import _Author, _plan_attack, _plan_competition, _plan_gate
from graphs.delivery.lifecycle_review import (
    _arbiter_scope,
    _claims,
    _clean_critique,
    _clean_gate_row,
    _critique,
    _handoff,
    _handoff_critique,
    _NodeFailure,
    _patch_truncated_review,
    _review_round,
    review_is_placeholder,
    round_summary,
)
from graphs.delivery.lifecycle_schemas import BUILD_SCHEMA, PLAN_SCHEMA, SCOPE_SCHEMA
from runner.decision_source import DecisionSource
from runner.protocol import BudgetStop, NodeRunner

__all__ = ["GRAPH_NAME", "review_is_placeholder", "run"]

GRAPH_NAME = "lifecycle-propose"


DEFAULT_FIX_ATTEMPTS = 2


def _ticket_text(ticket: Any, title: Any, body: Any) -> str:
    """Pure: the id, then the title and body when the harness supplied them."""
    parts = [str(ticket)]
    if title:
        parts[0] = f"{ticket} — {title}"
    if body:
        parts.append(str(body).strip())
    return "\n".join(parts)


# Two successive patches this similar are the same patch with the whitespace
# moved. 0.98 rather than 1.0 because a builder that re-emits its own diff
# rarely re-emits it byte-identically, and "it changed a comment" is not the
# objection falling.
NO_PROGRESS_RATIO = 0.98


def _infra_result(
    *, run_id: Any, date: Any, ticket: Any, scope: Mapping[str, Any] | None,
    build: Mapping[str, Any], handoff: Mapping[str, Any] | None, exc: _NodeFailure,
) -> dict[str, Any]:
    """The record for a task whose non-build node raised mid-round.

    Nothing here was judged, so there is no verdict and no proposal — only
    what survived the raise: the patch, whatever review the round reached
    before it failed, and which node failed.
    """
    return {
        "run_id": run_id,
        "date": date,
        "ticket": ticket,
        "scope": scope,
        "handoff": handoff,
        "adversary": exc.adversary,
        "arbitration": None,
        "build": dict(build),
        "review": exc.review,
        "proposals": [],
        "failed_node": exc.role,
        "failed_node_reason": str(exc),
    }


def _build_only_result(
    runner: NodeRunner, *, args: Mapping[str, Any], context: list[str], bound: Mapping[str, Any],
    scope: Mapping[str, Any] | None, plan: Mapping[str, Any], build: Mapping[str, Any], facts: Mapping[str, Any],
    frozen: bool, clean_finding: Any, ticket_text: str,
) -> dict[str, Any]:
    """The record of a build-only run (the phase lane's task build): plan, build and handoff, and no review of any
    kind, since on the phase model a task is never reviewed alone; the phase is (L6). A build the graph could not get
    into shape (`frozen`: truncated, invalid, stopped at a checkpoint) or that the clean check caught says why under
    `build_stopped`, and gets no handoff. So does a handoff that stopped the line or found the change incomplete:
    with no fix loop here, the lane builds the task again on its next claim."""
    stopped = "the build could not be put into shape" if frozen else (
        f"clean check: {clean_finding}" if clean_finding is not None else None
    )
    handoff: dict[str, Any] | None = None
    if stopped is None and "handoff" in bound:
        try:
            handoff = _handoff(runner, context=context, ticket=ticket_text, plan=plan, build=build, facts=facts,
                               ticket_id=args["ticket"])
        except _NodeFailure as exc:
            stopped = f"handoff: {exc}"
        else:
            if not handoff.get("complete"):
                stopped = f"handoff incomplete: {handoff.get('missing') or handoff}"
    return {
        "run_id": args["run_id"], "date": args["date"], "ticket": args["ticket"], "scope": scope, "plan": dict(plan),
        "build": dict(build), "handoff": handoff, "change_facts": dict(facts), "build_only": True,
        "build_stopped": stopped, "proposals": [],
    }


def run(
    args: Mapping[str, Any], runner: NodeRunner, decision_source: DecisionSource | None = None
) -> dict[str, Any]:
    """Run the graph. Every input arrives as an argument — no clock, no disk.

    `decision_source`, when given, is asked at every runner call. Without one
    no call carries a router decision.
    """
    ticket_tiers = dict(args.get("tier") or {})
    asking = runner if decision_source is None else _Asking(runner, decision_source)
    return _run(args, _Elevated(asking, ticket_tiers) if ticket_tiers else asking, ticket_tiers, raw_runner=runner)


def _run(
    args: Mapping[str, Any], runner: NodeRunner, ticket_tiers: Mapping[str, str], *, raw_runner: Any = None
) -> dict[str, Any]:
    """The graph body; `runner` already carries the ticket's tiers, `raw_runner` is the one before wrapping."""
    cartridge = require_cartridge(args)
    run_id, date, ticket = require(args, "run_id", "date", "ticket")
    # The work item's own words travel with its id. Traced plan nodes spent
    # their first turns globbing the repository for a file named like the
    # ticket, because the id was all they were given; a title and a body in
    # the prompt is the difference between a 10-turn plan and a 24-turn one.
    ticket_text = _ticket_text(ticket, args.get("ticket_title"), args.get("ticket_body"))

    # A per-call dollar ceiling for the build role. The harness has no
    # "float" kind (`Need` offers str, int, json_file, jsonl_file,
    # text_or_path — see graphs/_spec.py), so a CLI-supplied value arrives as
    # a raw string; the epic driver hands one straight off parsed YAML as a
    # float already. Coerced once, here, rather than trusted at each site
    # that passes it to the runner.
    raw_build_budget_usd = args.get("build_budget_usd")
    build_budget_usd = None if raw_build_budget_usd is None else float(raw_build_budget_usd)
    # "ticket" or "estimate" from the epic driver; None on the CLI path, where
    # a supplied budget really is the caller's override.
    build_budget_source = args.get("build_budget_source")

    is_work_item = bool(args.get("work_item"))

    context = list(cartridge.get("context") or [])
    proposals: list[dict[str, Any]] = []

    # Scoping is a SEPARATE ACT from filing, and it runs first: unscoped work
    # routes to the future-work landing area, never onto the active board.
    # Optional — a team that has not bound `scope_epic` simply does not get it,
    # which is what an optional role means.
    scope: dict[str, Any] | None = None
    if "scope_epic" in (cartridge.get("skills") or {}):
        scope = dict(
            runner.run(
                role="scope_epic",
                tier="standard",
                schema=SCOPE_SCHEMA,
                context=context,
                prompt=(
                    f"Scope this work.\n\nTicket: {ticket_text}\nDate: {date}\n\n"
                    "List the phases, the tickets, and the repositories it touches. "
                    "Say whether it is being worked now (active), scoped and scheduled "
                    "(planned), or roadmapped for later (future). Name an existing epic "
                    "to attach to if one covers this area."
                ),
            )
        )
        shape = epic_shape(
            cartridge,
            phases=len(scope.get("phases") or []),
            tickets=len(scope.get("tickets") or []),
            repos=len(scope.get("repos") or []),
        )
        landing = landing_for(cartridge, scope.get("state", "planned"))
        scope["shape"] = shape
        scope["landing"] = landing

        # A ticket that is already a work item must not propose filing itself: the
        # workstore arm has no apply block to route, falls back, and writes nothing.
        if not is_work_item:
            proposals.append(
                proposal(
                    cartridge,
                    kind="item_create",
                    target=str(scope.get("parent_epic") or ticket),
                    evidence=[
                        {"check": "epic_threshold", "output": f"{shape} ({len(scope.get('tickets') or [])} tickets, {len(scope.get('phases') or [])} phases, {len(scope.get('repos') or [])} repos)"},
                        {"check": "work_routing", "output": f"state '{scope.get('state')}' lands in {landing}"},
                    ],
                    rationale=str(scope.get("rationale", "")),
                    suggested_action=(
                        f"file as {shape} in {landing}"
                        + (f", attached to {scope['parent_epic']}" if scope.get("parent_epic") else "")
                    ),
                )
            )

    bound = cartridge.get("skills") or {}
    surfaces = list(args.get("surfaces") or [])
    patterns = list(args.get("patterns") or [])
    gate_tier, gate_min, competition_bound, attack_bound, compete = _plan_gate(cartridge, bound, surfaces, patterns)
    plan_gate = {
        "tier": gate_tier,
        "min_tier": gate_min,
        "competition": competition_bound and compete,
        "attack": attack_bound and compete,
        "ran": (competition_bound or attack_bound) and compete,
    }

    # The first planner keeps the ticket's own thread; build joins it later.
    author = _Author("plan", "standard", str(ticket))
    first_plan = runner.run(
        role=author.role,
        tier=author.tier,
        thread=author.thread,
        task=str(ticket),
        schema=PLAN_SCHEMA,
        context=context,
        prompt=(
            f"Decompose this ticket into an ordered plan.\n\nTicket: {ticket_text}\n"
            f"Date: {date}\n\nName the files you expect to touch, and state what is "
            "explicitly out of scope."
        ),
    )

    # The competition needs both halves: an alternative nobody judges is a
    # plan nobody builds, and an arbiter with one plan has nothing to decide.
    competition: dict[str, Any] | None = None
    if "plan_alternative" in bound and "plan_arbitrate" in bound and compete:
        chosen_plan, competition, author = _plan_competition(
            runner, context=context, ticket=ticket_text, date=date, plan=first_plan, first=author, task_id=ticket
        )
    else:
        chosen_plan = dict(first_plan)

    # A revision goes to whoever wrote the chosen plan, on that seat's thread.
    plan_attack: dict[str, Any] | None = None
    if "plan_adversary" in bound and compete:
        plan, plan_attack = _plan_attack(
            runner, context=context, ticket=ticket_text, author=author, plan=chosen_plan, task_id=ticket
        )
    else:
        plan = chosen_plan

    # Plan, build and the fix-loop retry share one thread: the builder starts
    # from what the planner already read, and a retry from a tree it already
    # edited. Review never joins the thread — a reviewer that inherits the
    # builder's reasoning is the failure the seat exists to prevent.
    continuations = 0
    continuation_refused: str | None = None
    checkpoint = _checkpoint_context(cartridge, raw_runner, run_id, ticket)
    previous_checkpoint_patch: str | None = None
    checkpoint_reason: str | None = None
    try:
        build = runner.run(
            role="build",
            tier="standard",
            thread=str(ticket),
            task=str(ticket),
            schema=BUILD_SCHEMA,
            context=context,
            budget_usd=build_budget_usd,
            prompt=(
                f"Carry out this plan and return the change as a unified diff.\n\n"
                f"Ticket: {ticket_text}\nPlan: {plan}\n\nReturn the patch only — it is applied "
                "by the shell into a worktree, never by you. No tags, no fences, no trailing "
                "markup of any kind — the text is fed to `git apply` verbatim and a stray "
                "`</patch>` fails the checks. Include the deterministic "
                "commands you ran and their output."
            ),
        )
    except BudgetStop as exc:
        resumed, continuations, reason, stop, previous_checkpoint_patch, checkpoint_reason = _resume_build(
            runner, context=context, ticket=ticket, budget_usd=build_budget_usd,
            surfaces=surfaces, stop=exc, continuations=continuations,
            checkpoint=checkpoint, previous_checkpoint_patch=previous_checkpoint_patch,
        )
        if resumed is None:
            if checkpoint_reason is not None:
                build = {"patch": stop.partial_patch or "", "summary": "", "files_touched": [], "commands_run": []}
            else:
                # There is no reviewed build yet to keep — nothing to idle. The
                # reason travels on the exception itself, since no result is
                # returned for a `continuation_refused` field to live on.
                raise BudgetStop(
                    role=stop.role,
                    thread=stop.thread,
                    session=stop.session,
                    spent_usd=stop.spent_usd,
                    partial_patch=stop.partial_patch,
                    detail=f"{stop.detail} — continuation refused: {reason}",
                ) from stop
        else:
            build = resumed

    # A patch that does not parse is asked again once, with the reason
    # attached, and never chased further: the second answer is final.
    truncation = patch_parses(build.get("patch") or "") if checkpoint_reason is None else None
    if truncation is not None:
        try:
            build = runner.run(
                role="build",
                tier="standard",
                thread=str(ticket),
                task=str(ticket),
                schema=BUILD_SCHEMA,
                context=context,
                budget_usd=build_budget_usd,
                prompt=(
                    f"Carry out this plan and return the change as a unified diff.\n\n"
                    f"Ticket: {ticket_text}\nPlan: {plan}\n\nReturn the patch only — it is applied "
                    "by the shell into a worktree, never by you. No tags, no fences, no trailing "
                    "markup of any kind — the text is fed to `git apply` verbatim and a stray "
                    "`</patch>` fails the checks. Include the deterministic "
                    f"commands you ran and their output. {truncation}"
                ),
            )
        except BudgetStop as exc:
            resumed, continuations, reason, stop, previous_checkpoint_patch, checkpoint_reason = _resume_build(
                runner, context=context, ticket=ticket, budget_usd=build_budget_usd,
                surfaces=surfaces, stop=exc, continuations=continuations,
                checkpoint=checkpoint, previous_checkpoint_patch=previous_checkpoint_patch,
            )
            if resumed is None:
                if checkpoint_reason is not None:
                    build = {"patch": stop.partial_patch or "", "summary": "", "files_touched": [], "commands_run": []}
                else:
                    raise BudgetStop(
                        role=stop.role,
                        thread=stop.thread,
                        session=stop.session,
                        spent_usd=stop.spent_usd,
                        partial_patch=stop.partial_patch,
                        detail=f"{stop.detail} — continuation refused: {reason}",
                    ) from stop
            else:
                build = resumed
        truncation = patch_parses(build.get("patch") or "") if checkpoint_reason is None else None
    patch_truncated = truncation is not None

    # A patch whose files diverge from `files_touched`, or whose contract
    # commands are missing or unevidenced, is asked again once, named.
    invalid = None if (patch_truncated or checkpoint_reason is not None) else _build_output_valid(build, ticket_text)
    if invalid is not None:
        try:
            build = runner.run(
                role="build", hints=_hints(str(build.get("patch") or "")),
                thread=str(ticket), task=str(ticket), schema=BUILD_SCHEMA,
                context=context, budget_usd=build_budget_usd,
                prompt=(
                    f"Carry out this plan and return the change as a unified diff.\n\n"
                    f"Ticket: {ticket_text}\nPlan: {plan}\n\n{invalid} Return the patch only — it is "
                    "applied by the shell into a worktree, never by you."
                ),
            )
        except BudgetStop as exc:
            resumed, continuations, reason, stop, previous_checkpoint_patch, checkpoint_reason = _resume_build(
                runner, context=context, ticket=ticket, budget_usd=build_budget_usd,
                surfaces=surfaces, stop=exc, continuations=continuations,
                checkpoint=checkpoint, previous_checkpoint_patch=previous_checkpoint_patch,
            )
            if resumed is None:
                if checkpoint_reason is not None:
                    build = {"patch": stop.partial_patch or "", "summary": "", "files_touched": [], "commands_run": []}
                else:
                    raise BudgetStop(
                        role=stop.role, thread=stop.thread, session=stop.session, spent_usd=stop.spent_usd,
                        partial_patch=stop.partial_patch, detail=f"{stop.detail} — continuation refused: {reason}",
                    ) from stop
            else:
                build = resumed
        invalid = _build_output_valid(build, ticket_text) if checkpoint_reason is None else None
    build_output_invalid = invalid is not None
    frozen = patch_truncated or build_output_invalid or checkpoint_reason is not None

    # A hit on the clean check is a build fault the builder can fix, so it is
    # caught here, before the handoff and before any reviewer reads the patch.
    clean_finding, clean_status = (
        (None, "none")
        if frozen
        else clean_gate_outcome(build.get("patch") or "", checkpoint.checks, checkpoint.repo_dir)
    )
    clean_gate_row = _clean_gate_row(1, clean_finding, clean_status)

    facts = _change_facts(build)
    tier = review_tier(cartridge, change_facts=facts, surfaces=surfaces, patterns=patterns)

    if args.get("build_only"):
        return _build_only_result(
            runner, args=args, context=context, bound=bound, scope=scope, plan=plan, build=build, facts=facts,
            frozen=frozen, clean_finding=clean_finding, ticket_text=ticket_text,
        )

    handoff: dict[str, Any] | None = None
    # Declared here, not beside `standing`: the first pass below appends to it.
    rounds: list[dict[str, Any]] = []
    try:
        if not frozen and clean_finding is None and "handoff" in bound:
            handoff = _handoff(runner, context=context, ticket=ticket_text, plan=plan, build=build, facts=facts, ticket_id=ticket)

        # A non-blocking refusal costs a build attempt, not the run. Review is
        # skipped — there is nothing yet worth a deep-tier opinion — and the
        # handoff's own list of what is missing is what the builder is sent back
        # with. Paying two reviewers to read a change the shuttle already said is
        # under-evidenced would buy an opinion about the wrong thing. A patch still
        # truncated after its one retry never buys a review either — there is
        # nothing yet that applies.
        if checkpoint_reason is not None:
            review = {
                "verdict": "revise",
                "rationale": checkpoint_reason,
                "findings": [{"charter_principle": "checkpoint", "detail": checkpoint_reason, "file": ""}],
            }
            adversary, arbitration, verdict, review_placeholder, review_quarantine = None, None, "revise", False, False
        elif patch_truncated:
            review, adversary, arbitration = _patch_truncated_review(truncation), None, None
            verdict, review_placeholder, review_quarantine = "revise", False, False
        elif build_output_invalid:
            review = {"verdict": "revise", "rationale": "build_output_invalid",
                      "findings": [{"charter_principle": "harness fault", "detail": invalid, "file": ""}]}
            adversary, arbitration, verdict, review_placeholder, review_quarantine = None, None, "revise", False, False
        elif clean_finding is not None:
            review, adversary, arbitration = _clean_critique(clean_finding), None, None
            verdict, review_placeholder, review_quarantine = "revise", False, False
            rounds.append(round_summary(1, "clean check", review, adversary, arbitration, verdict))
        elif handoff is not None and not handoff.get("complete"):
            review, adversary, arbitration, verdict, review_placeholder, review_quarantine = _handoff_critique(handoff)
            rounds.append(round_summary(1, "handoff", review, adversary, arbitration, verdict))
        else:
            review, adversary, arbitration, verdict, review_placeholder, review_quarantine = _review_round(
                runner,
                context=context,
                bound=bound,
                ticket=ticket_text,
                build=build,
                facts=facts,
                handoff=handoff,
                tier=tier,
                attempt=1,
                task_id=ticket,
            )
            rounds.append(round_summary(1, "review", review, adversary, arbitration, verdict))
    except _NodeFailure as exc:
        return _infra_result(run_id=run_id, date=date, ticket=ticket, scope=scope, build=build, handoff=handoff, exc=exc)
    # Carried across every round: an abstention two rounds ago is still an
    # abstention, even once a later round comes back clean.
    any_review_placeholder = review_placeholder

    # The bounded fix loop. A change sent back goes back to the builder with the
    # critique attached — but the loop is bounded in three separate ways, because
    # an unbounded one is just a machine for grinding a change past its reviewers
    # until someone blinks.
    fix_attempts = args.get("fix_attempts")
    fix_attempts = DEFAULT_FIX_ATTEMPTS if fix_attempts is None else int(fix_attempts)
    attempts = 1
    # Both reviewers abstaining is a harness fault, not a task to rebuild
    # against — the loop never gets a chance to start. A patch truncated twice
    # is the same shape: nothing to send back and revise.
    stopped: str | None = (
        "checkpoint" if checkpoint_reason is not None
        else "patch_truncated" if patch_truncated
        else "build_output_invalid" if build_output_invalid
        else "harness fault: review placeholders" if review_quarantine
        else None
    )
    standing: set[str] = set()
    # Whether the round that produced the current `verdict` ended at handoff
    # (no reviewer has read this patch yet) rather than at a review verdict.
    prior_handoff = handoff is not None and not handoff.get("complete")
    # Whether the latest round ended at a clean-check hit: no reviewer read it either.
    prior_clean = clean_finding is not None
    # Set when the second build asks a tier up. The runner takes no `reason`,
    # so the escalation is told on the record, as an evidence row, not to the call.
    escalation: str | None = None

    while verdict != "approve" and attempts <= fix_attempts and not review_quarantine and not frozen:
        # Every claim raised so far, not merely the last round's. Re-raising an
        # objection from two rounds ago is no more progress than re-raising the
        # one from the last.
        standing |= _claims(adversary)
        critique = _critique(review, adversary, arbitration)

        # Only the second build, and only after a review revise: not a handoff
        # gap or a clean-check hit, and never a third attempt. A ticket tier for
        # build overrides it.
        escalate = (
            int(attempts) == 1 and verdict == "revise" and not prior_handoff and not prior_clean
            and "build" not in ticket_tiers
        )
        retry_tier = _escalate("standard") if escalate else "standard"
        if escalate:
            escalation = f"escalated: attempt 2 after revise (standard -> {retry_tier})"

        try:
            retry = runner.run(
                role="build",
                tier=retry_tier,
                thread=str(ticket),
                task=str(ticket),
                schema=BUILD_SCHEMA,
                context=context,
                budget_usd=build_budget_usd,
                prompt=(
                    "This change was sent back. Start from the previous patch — apply it "
                    "first, then change only what the critique requires — and return a new "
                    "unified diff of the whole change.\n\n"
                    f"Ticket: {ticket_text}\nPlan: {plan}\n\n"
                    f"Previous patch (apply this first; do not redo the work it already did):\n"
                    f"{build.get('patch')}\n\n"
                    f"Standing critique:\n{critique}\n\n"
                    "Every objection above must actually fall — a patch that leaves one "
                    "of them standing is not a fix, and saying it is addressed is not the "
                    "same as addressing it. Return the patch only — it is applied by the "
                    "shell into a worktree, never by you. No tags, no fences, no trailing "
                    "markup of any kind — the text is fed to `git apply` verbatim and a "
                    "stray `</patch>` fails the checks. Include the deterministic "
                    "commands you ran and their output."
                ),
            )
        except BudgetStop as exc:
            resumed, continuations, reason, _stop, previous_checkpoint_patch, stop_checkpoint_reason = _resume_build(
                runner, context=context, ticket=ticket, budget_usd=build_budget_usd,
                surfaces=surfaces, stop=exc, continuations=continuations, tier=retry_tier,
                checkpoint=checkpoint, previous_checkpoint_patch=previous_checkpoint_patch,
            )
            if resumed is None:
                # The retry spent the budget without returning a patch, and a
                # continuation was refused (or a checkpoint revised). `build`
                # and `review` still describe the last patch actually
                # reviewed — that is the thing worth keeping, not an
                # exception that loses it along with everything the run
                # already earned.
                attempts += 1
                if stop_checkpoint_reason is not None:
                    stopped = "checkpoint"
                    checkpoint_reason = stop_checkpoint_reason
                else:
                    stopped = "budget"
                    continuation_refused = reason
                break
            retry = resumed
        attempts += 1

        # No progress: a revise arbitration is judged by whether its scoped
        # files' own diff text moved, not the whole patch; no scope means any file.
        scope = _arbiter_scope(verdict, arbitration)
        if scope is None:
            no_progress = SequenceMatcher(None, build.get("patch") or "", retry.get("patch") or "").ratio() >= NO_PROGRESS_RATIO
        else:
            prior, current = _patch_sections(build.get("patch") or ""), _patch_sections(retry.get("patch") or "")
            # A scope naming files neither patch touches would zero progress
            # on nothing; only the scoped files that exist in a patch count,
            # and none left means any file.
            touched = (scope & (set(prior) | set(current)) if scope else set()) or set(prior) | set(current)
            no_progress = not any(prior.get(f) != current.get(f) for f in touched)
        # A resubmission after handoff evidence, not a review revise, is progress
        # the moment the evidence itself moved, even with the diff unchanged.
        if prior_handoff and no_progress:
            no_progress = retry.get("commands_run") == build.get("commands_run") and retry.get("summary") == build.get("summary")
        if no_progress:
            # The retry is dropped rather than returned: `build` and `review`
            # must describe the same patch, or the record lies about what was
            # reviewed. The attempt is still counted — it was still spent.
            stopped = "no_progress"
            break

        build = retry
        facts = _change_facts(build)
        # The same gate on every retry, before that round's handoff and review:
        # a retry that still carries the hit is flagged again, with no reviewer paid.
        clean_finding, clean_status = clean_gate_outcome(build.get("patch") or "", checkpoint.checks, checkpoint.repo_dir)
        clean_gate_row = _clean_gate_row(int(attempts), clean_finding, clean_status) or clean_gate_row
        prior_clean = clean_finding is not None
        if clean_finding is not None:
            review, adversary, arbitration = _clean_critique(clean_finding), None, None
            verdict, review_placeholder, review_quarantine = "revise", False, False
            rounds.append(round_summary(int(attempts), "clean check", review, adversary, arbitration, verdict))
            prior_handoff = False
            continue
        try:
            if "handoff" in bound:
                handoff = _handoff(runner, context=context, ticket=ticket_text, plan=plan, build=build, facts=facts, ticket_id=ticket)
                # Still under-evidenced. The same rule as the first pass: another
                # attempt if the cap allows one, and never a review round bought
                # for a change the shuttle has already refused to hand over.
                if not handoff.get("complete"):
                    review, adversary, arbitration, verdict, review_placeholder, review_quarantine = _handoff_critique(handoff)
                    rounds.append(round_summary(int(attempts), "handoff", review, adversary, arbitration, verdict))
                    any_review_placeholder = any_review_placeholder or review_placeholder
                    prior_handoff = True
                    # No reviewer has seen this build yet, so the round costs
                    # half an attempt, not a whole one — two of these plus a
                    # real review round must not exhaust the cap first.
                    attempts -= 0.5
                    continue
            prior_handoff = False
            tier = review_tier(cartridge, change_facts=facts, surfaces=surfaces, patterns=patterns)
            review, adversary, arbitration, verdict, review_placeholder, review_quarantine = _review_round(
                runner,
                context=context,
                bound=bound,
                ticket=ticket_text,
                build=build,
                facts=facts,
                handoff=handoff,
                tier=tier,
                attempt=int(attempts),
                task_id=ticket,
            )
            rounds.append(round_summary(int(attempts), "review", review, adversary, arbitration, verdict))
        except _NodeFailure as exc:
            return _infra_result(run_id=run_id, date=date, ticket=ticket, scope=scope, build=build, handoff=handoff, exc=exc)
        any_review_placeholder = any_review_placeholder or review_placeholder
        if review_quarantine:
            stopped = "harness fault: review placeholders"
            break

        # An approval here is not a technicality. The reviewers saw the standing
        # objections in the patch they were given and approved anyway, which is
        # them judging the objections fallen. Their call, not the loop's.
        if verdict == "approve":
            break

        if standing & _claims(adversary):
            stopped = "objection_standing"
            break

    if verdict != "approve" and stopped is None:
        stopped = "attempts_exhausted"

    if verdict == "approve":
        # A draft PR has no effect until someone opens it, which is why it is the
        # one kind that starts eligible. It is still emitted, never executed.
        proposals.append(
            proposal(
                cartridge,
                kind="draft_pr_create",
                target=str(ticket),
                evidence=[
                    {"check": "review tier", "output": str(tier)},
                    # Only when a gated seat is bound but the tier never cleared
                    # the floor: a row that always reads "ran" is a row nobody
                    # reads, and a row present whether or not a seat is bound
                    # would claim a skip that was never even offered.
                    # One row per seat that was bound and skipped, named for what
                    # it is: a cartridge with only the attacker bound never had
                    # a competition to skip.
                    *(
                        [{"check": "plan gate", "output": f"tier {gate_tier} vs min {gate_min}: competition skipped"}]
                        if competition_bound and not compete
                        else []
                    ),
                    *(
                        [{"check": "plan gate", "output": f"tier {gate_tier} vs min {gate_min}: plan attack skipped"}]
                        if attack_bound and not compete
                        else []
                    ),
                    # Only when a competition or an attack ran: a row that
                    # always reads "no competition" is a row nobody reads.
                    *(
                        [{"check": "plan competition", "output": f"chose {competition['chosen']}: {competition['reasoning']} (price: {competition['price']})"}]
                        if competition
                        else []
                    ),
                    *(
                        [{"check": "plan adversary", "output": f"{plan_attack['attack'].get('verdict')} — strongest: {plan_attack['attack'].get('strongest_objection')}" + (" — plan revised once" if plan_attack["revised"] else "")}]
                        if plan_attack
                        else []
                    ),
                    {"check": "review_charter verdict", "output": str(review.get("verdict"))},
                    *(
                        [{"check": "adversary verdict", "output": str(adversary.get("verdict"))},
                         {"check": "strongest objection", "output": str(adversary.get("strongest_objection"))}]
                        if adversary
                        else []
                    ),
                    *(
                        [{"check": "arbitration", "output": f"{arbitration.get('sided_with')}: {arbitration.get('reasoning')}"}]
                        if isinstance(arbitration, Mapping) and arbitration
                        else [{"check": "arbitration", "output": arbitration}] if arbitration
                        else []
                    ),
                    # Only when there was a loop. A first-try approval says
                    # nothing about a fix loop because there was not one, and a
                    # row reading "attempt 1 of 3" on every clean pass is a row
                    # that stops being read.
                    *(
                        [{"check": "fix loop", "output": f"approved on attempt {attempts} of {fix_attempts + 1}"}]
                        if attempts > 1
                        else []
                    ),
                    # Only when the gate ran or skipped: a row for a gate with
                    # nothing to run on would claim a check that never happened.
                    *([clean_gate_row] if clean_gate_row else []),
                    *([{"check": "tier escalation", "output": escalation}] if escalation else []),
                    {"check": "changed lines", "output": str(facts["changed_lines"])},
                    # Only when a budget was set, and attributed to where it
                    # came from: an estimated budget read as an override would
                    # claim an override that never happened. No source means
                    # the CLI flag, which is an override.
                    *(
                        [
                            {
                                "check": "build budget",
                                "output": (
                                    f"budget ${build_budget_usd} from {build_budget_source}"
                                    if build_budget_source
                                    else f"override ${build_budget_usd} per build call"
                                ),
                            }
                        ]
                        if build_budget_usd is not None
                        else []
                    ),
                    # Normalised into the evidence shape rather than spread raw:
                    # a commands_run entry is keyed `command`, and everything
                    # downstream — the gate, the manifest — reads `check`.
                    *(
                        {"check": entry.get("command"), "output": entry.get("output")}
                        for entry in build.get("commands_run", [])
                        if isinstance(entry, Mapping)
                    ),
                ],
                rationale=review.get("rationale", ""),
                suggested_action=f"open a draft PR for {ticket} from the build worktree",
                # Carried only when it happened, and then always. The ledger
                # cannot refuse to extend a streak on a repeated-attempt pass if
                # the pass never told it there was one.
                attempts=attempts if attempts > 1 else None,
            )
        )

    return {
        "run_id": run_id,
        "date": date,
        "ticket": ticket,
        "scope": scope,
        "review_tier": tier,
        "handoff": handoff,
        "adversary": adversary,
        "arbitration": arbitration,
        "plan": dict(plan),
        "plan_competition": competition,
        "plan_attack": plan_attack,
        "plan_gate": plan_gate,
        "build": dict(build),
        "review": dict(review),
        "change_facts": facts,
        "fix_loop": {
            "attempts": attempts,
            "stopped": stopped,
            "continuations": continuations,
            "rounds": rounds,
            **({"continuation_refused": continuation_refused} if continuation_refused is not None else {}),
            **({"checkpoint_reason": checkpoint_reason} if checkpoint_reason is not None else {}),
            **({"review_placeholder": True} if any_review_placeholder else {}),
        },
        "proposals": proposals,
    }


from graphs._spec import GraphSpec, Need  # noqa: E402

SPEC = GraphSpec(
    name="lifecycle",
    graph_name=GRAPH_NAME,
    run=run,
    summary="the development loop: scope, plan, build, review — proposals out, nothing pushed",
    needs=(
        Need("ticket", flag="--ticket", help="the ticket to work"),
        Need("fix_attempts", flag="--fix-attempts", kind="int", required=False,
             help="additional build attempts after the first (default 2); 0 disables the fix loop"),
        Need("build_budget_usd", flag="--build-budget-usd", required=False,
             help="a per-call dollar ceiling for the build role, overriding the default "
                  "(a plain number; there is no float kind, so it arrives as a string "
                  "and this graph coerces it)"),
    ),
)
