"""lifecycle-propose's review rounds: the handoff check, the reviewers, the adversary and the arbiter.

Also the measured facts and evidence rules the handoff judges a build by, and the critique a revise hands back.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast

from graphs._contract import (
    ContractViolation,
)
from graphs.delivery.clean_gate import GateStatus
from graphs.delivery.lifecycle_patch import _hints
from graphs.delivery.lifecycle_schemas import ADVERSARY_SCHEMA, ARBITRATE_SCHEMA, HANDOFF_SCHEMA, REVIEW_SCHEMA
from graphs.delivery.phase_validate import _PLACEHOLDER_MARKERS
from runner.protocol import BudgetStop, NodeRunner, RunnerError
from runner.system_one import Answer
from runner.tier_resolution import Hints

# How much of a patch the handoff sees: all of it, up to a bound that only a
# pathological diff reaches. A 6,000-character preview was tried first and the
# handoff — correctly — refused every patch it could see was cut off. The
# shuttle judges the cargo; it cannot judge half of it.
PATCH_PREVIEW_CHARS = 200_000


def harness_verify_block(build: Mapping[str, Any]) -> str:
    """The `harness_verify` rows, quoted verbatim for a reviewer prompt; "" when there are none."""
    rows = [
        entry
        for entry in build.get("commands_run") or []
        if isinstance(entry, Mapping) and entry.get("source") == "harness_verify"
    ]
    if not rows:
        return ""
    quoted = "\n".join(f"$ {row.get('command')}\n{row.get('output')}" for row in rows)
    return (
        "Harness-run verify evidence: the harness ran these commands in the build's own scratch "
        "after the patch. They are not builder claims. Quoted verbatim:\n" + quoted + "\n"
    )


def measured_facts(build: Mapping[str, Any], change_facts: Mapping[str, Any]) -> dict[str, str]:
    """The facts the harness itself established, keyed for `prune_missing` to cite.

    Every path the build touched, the five line counts, and the tail of every
    `pytest` command it ran — all of it already measured, none of it asked of a
    model. A `missing` item that names one of these is asking for a number the
    graph is already holding.
    """
    facts: dict[str, str] = {}
    for path in change_facts.get("files_touched") or []:
        facts[f"file:{path}"] = f"touched: {path}"
    for key in ("added_lines", "removed_lines", "changed_lines", "source_lines", "test_lines"):
        facts[key] = f"{key.replace('_', ' ')}: {change_facts.get(key)}"
    for index, entry in enumerate(build.get("commands_run") or []):
        command = str(entry.get("command") or "") if isinstance(entry, Mapping) else ""
        if not command.startswith("pytest"):
            continue
        output = str(entry.get("output") or "") if isinstance(entry, Mapping) else ""
        tail = "\n".join(output.strip().splitlines()[-3:])
        facts[f"pytest:{index}"] = f"{command} -> {tail}"
    return facts


# The model's own words for "I already have this"; matched case-insensitively
# against a `missing` item, then against the fact-key substring that answers
# it. A complaint typed differently still names the same absent thing.
_DISCHARGE_PHRASES = {
    "files touched": "file",
    "git diff": "file",
    "diff shown": "file",
    "line count": "lines",
    "lines": "lines",
    "size": "lines",
    "budget": "lines",
    "pytest": "pytest",
    "test output": "pytest",
    "suite": "pytest",
    "passed": "pytest",
}


def prune_missing(missing: list[str], facts: dict[str, str]) -> tuple[list[str], list[str]]:
    """Split a model's `missing` list into what still stands and what the
    harness already answered.

    A complaint that names a fact the harness already measured is discharged,
    paired with the fact that answers it — a reader sees the harness answered
    it rather than a build attempt spent closing a gap that was never open.
    """
    kept: list[str] = []
    discharged: list[str] = []
    for item in missing:
        # Word-bounded: a phrase must appear as itself, not as a run of
        # letters inside a longer word — "cleanliness" is not "lines".
        substring = next(
            (sub for phrase, sub in _DISCHARGE_PHRASES.items() if re.search(rf"\b{re.escape(phrase)}\b", item, re.IGNORECASE)),
            None,
        )
        fact = next((value for key, value in facts.items() if substring and substring in key), None)
        if fact is not None:
            discharged.append(f"{item} -> {fact}")
        else:
            kept.append(item)
    return kept, discharged


_COMMAND_WORDS = frozenset({"uv", "python", "python3", "grep", "rg", "make", "npm", "bash", "sh", "cat", "sed", "cox", "git"})


_BACKTICK_SPAN_RE = re.compile(r"`([^`]+)`")


def impossible_evidence(item: str, prefixes: Sequence[str]) -> bool:
    """True when a backticked command in `item` starts with none of the build session's permitted `prefixes`."""
    spans = (span.strip() for span in _BACKTICK_SPAN_RE.findall(item))
    return any(
        span.split()[0] in _COMMAND_WORDS and not any(span.startswith(prefix) for prefix in prefixes)
        for span in spans
        if span
    )


_SIZE_TARGET_RE = re.compile(r"~\s*(\d+)\s*lines", re.IGNORECASE)


def _size_deviation(ticket_text: str, source_lines: int, test_lines: int) -> str | None:
    """Overrun of `source_lines` against the ticket's `~N lines` target; test lines are reported, not counted."""
    match = _SIZE_TARGET_RE.search(ticket_text)
    if not match:
        return None
    target = int(match.group(1))
    if source_lines <= target:
        return None
    return f"deviation: {source_lines} source lines against ~{target} (tests {test_lines} lines, not counted)"


def _test_ratio_deviation(source_lines: int, test_lines: int) -> str | None:
    """A disclosed deviation, never a refusal, when tests dwarf the source they cover."""
    if source_lines <= 0 or test_lines <= 3 * source_lines:
        return None
    return f"deviation: tests are {test_lines / source_lines:.1f}x the source"


def _claims(adversary: Mapping[str, Any] | None) -> set[str]:
    """The adversary's objections, normalised for comparison across rounds.

    Case and surrounding whitespace are not the objection. The same complaint
    typed differently in the next round is still the same complaint, still
    standing — and a comparison strict enough to miss that would let a loop
    re-litigate one objection until the cap ran out.
    """
    if not adversary:
        return set()
    return {
        str(objection.get("claim") or "").strip().lower()
        for objection in adversary.get("objections") or []
        if isinstance(objection, Mapping) and str(objection.get("claim") or "").strip()
    }


def round_summary(
    attempt: int,
    source: str,
    review: Mapping[str, Any],
    adversary: Mapping[str, Any] | None,
    arbitration: Mapping[str, Any] | str | None,
    verdict: str,
) -> dict[str, Any]:
    """One handoff or review round, reduced to who spoke and how it ended.

    A skipped-arbiter marker is a string, not a decision, and reads as None.
    """
    return {
        "attempt": attempt,
        "source": source,
        "verdict": verdict,
        "findings": [str(f.get("charter_principle")) for f in review.get("findings") or [] if isinstance(f, Mapping)],
        "objections": len(adversary.get("objections") or []) if adversary else 0,
        "arbitration": (
            arbitration.get("decision", arbitration.get("verdict")) if isinstance(arbitration, Mapping) else None
        ),
    }


def _arbiter_scope(verdict: str, arbitration: Mapping[str, Any] | None) -> set[str] | None:
    """Backticked, path-shaped tokens in a revise arbitration's `reasoning`
    (a `/` or a file suffix); None outside that case. Backticked identifiers
    such as a renamed key are house style, not scope — a reasoning that
    names none reads as an empty scope, which means any file."""
    if verdict != "revise" or not arbitration:
        return None
    tokens = re.findall(r"`([^`\s]+)`", str(arbitration.get("reasoning") or ""))
    return {t for t in tokens if _PATH_TOKEN.match(t) and ("/" in t or re.search(r"\.[A-Za-z0-9]{1,5}$", t))}


# A scope entry is a repository path, nothing else: `glob("*/*.json")` and
# `f.stem` carry a slash or a suffix but are code, and admitting them once
# scoped a real revision to files nobody could touch — which is no_progress.
_PATH_TOKEN = re.compile(r"^[\w.\-]+(?:/[\w.\-]+)*$")


def _critique(
    review: Mapping[str, Any],
    adversary: Mapping[str, Any] | None,
    arbitration: Mapping[str, Any] | None,
) -> str:
    """Everything the reviewers held against the change, as one block of text.

    The whole critique, not a summary of it. A builder handed "review asked for
    changes" will fix the thing it already thought was wrong; a builder handed
    the objection verbatim has to answer that objection.
    """
    lines = [f"Charter reviewer: {review.get('verdict')} — {review.get('rationale')}"]
    lines += [
        f"- finding ({finding.get('charter_principle')}) in {finding.get('file')}: {finding.get('detail')}"
        for finding in review.get("findings") or []
        if isinstance(finding, Mapping)
    ]
    if adversary is not None:
        lines.append(f"Adversary: {adversary.get('verdict')} — strongest: {adversary.get('strongest_objection')}")
        lines += [
            f"- objection: {objection.get('claim')} — {objection.get('why_wrong')}"
            for objection in adversary.get("objections") or []
            if isinstance(objection, Mapping)
        ]
    if isinstance(arbitration, Mapping):
        lines.append(f"Arbitration sided with {arbitration.get('sided_with')}: {arbitration.get('reasoning')}")
    return "\n".join(lines)


def _handoff(
    runner: NodeRunner,
    *,
    context: list[str],
    ticket: Any,
    plan: Mapping[str, Any],
    build: Mapping[str, Any],
    facts: Mapping[str, Any],
    ticket_id: Any = None,
) -> dict[str, Any]:
    """The shuttle. Between build and review, someone checks that what came out
    of the last step is actually what the next one needs — and stops here if it
    is not. A review of a half-finished change produces a confident opinion
    about the wrong thing.

    A retry gets exactly the same check as the first try. An incomplete second
    attempt is still incomplete, and "we were already fixing it" is not a reason
    to review a gap.

    But an incomplete handoff is not one thing. Stopping the line is right when
    the artifact or an input is missing, because no amount of rebuilding
    conjures an input nobody produced. It is wrong when the patch is there and
    what is missing is evidence about it: that is a gap one more build attempt
    closes, and quarantining it throws away a finished change and buys a whole
    replan to get back to where the run already was. Three of four handoff stops
    in one day were the second kind. So this raises only on the blocking kind,
    and returns the incomplete handoff otherwise for the caller to route into
    the fix loop.

    Whether the artifact is present is decided HERE, from the patch, and only
    then refined by the node's own flag: a shuttle cannot truthfully call an
    artifact missing while holding it, and a graph that took its word for it
    would be re-deciding a deterministic fact with a model call.
    """
    # The artifact travels with the question. An earlier version handed the
    # handoff only the summary, the file list and the line counts — and it
    # correctly refused every build for "no patch text was handed off", five
    # epics running. A shuttle that cannot see the cargo cannot judge it.
    patch = str(build.get("patch") or "")
    try:
        handoff = dict(
            runner.run(
                role="handoff",
                tier="standard",
                task=None if ticket_id is None else str(ticket_id),
                schema=HANDOFF_SCHEMA,
                context=context,
                prompt=(
                    "The build step is done and the review step is next. Does what "
                    "build produced actually contain what a reviewer needs?\n\n"
                    f"Task: {ticket}\nPlan: {plan}\nSummary: {build.get('summary')}\n"
                    f"Files: {build.get('files_touched')}\nChange facts: {facts}\n"
                    "The facts listed under Change facts are already measured by the "
                    "harness; do not list any of them as missing. Any size target in "
                    "the ticket binds source lines only — test lines are reported "
                    "here, never a missing item.\n"
                    f"Commands run (with their real output): {build.get('commands_run')}\n"
                    "Rows with source harness_verify were run by the harness in the build's own "
                    "scratch after the patch, not by the builder; they are the ticket's verify: evidence. "
                    "Both reviewers receive them verbatim.\n"
                    f"Patch ({len(patch)} chars, {'complete' if len(patch) <= PATCH_PREVIEW_CHARS else 'head shown'}):\n"
                    f"{patch[:PATCH_PREVIEW_CHARS]}\n\n"
                    "List anything missing, and compress the rest into the smallest "
                    "brief that lets review start. The patch above IS the artifact under "
                    "review: judge whether it and the command evidence are sufficient, not "
                    "whether a repository somewhere already contains them.\n\n"
                    "If it is not complete, say whether the refusal is BLOCKING. Blocking "
                    "means the artifact itself or an input the plan needed is absent — "
                    "there is no change here, or the work depended on something nobody "
                    "produced, and no amount of rebuilding will conjure it. Not blocking "
                    "means the change is present and what it lacks is evidence about it: "
                    "a check nobody ran, output nobody attached, a claim nobody tested. "
                    "That second kind buys one more build attempt; it does not stop the "
                    "line, so do not mark it blocking to signal that it matters."
                ),
            )
        )
    except BudgetStop:
        # A budget stop is not a node failure — the CLI can resume this
        # session in a later phase, the same continuation the build node
        # already gets. Left to propagate unconverted, so it still reaches
        # `invoke_graphs` and quarantines `no_work` exactly as before this
        # change; retrying a non-build node's session is a sibling ticket's.
        raise
    except RunnerError as exc:
        raise _NodeFailure("handoff", exc) from exc
    # Anything the model flagged as missing that the harness already measured
    # is discharged here, not argued with the model: the fact is not absent,
    # it is sitting in `facts` unread. If discharging clears the list, the
    # handoff was complete all along.
    kept, discharged = prune_missing(list(handoff.get("missing") or []), measured_facts(build, facts))
    # Evidence that asks for a command the build's session cannot run is not a
    # gap another build attempt can close; discharge it rather than loop on it.
    permitted = getattr(runner, "permitted_prefixes", None)
    prefixes: Sequence[str] = cast("Sequence[str]", permitted()) if callable(permitted) else []
    if prefixes:
        impossible = [item for item in kept if impossible_evidence(item, prefixes)]
        kept = [item for item in kept if item not in impossible]
        discharged = [*discharged, *(f"{item} (not runnable in the build's session)" for item in impossible)]
    handoff["missing"] = kept
    handoff["discharged"] = discharged
    if not kept:
        handoff["complete"] = True
        handoff["blocking"] = False
    source_lines = int(facts.get("source_lines") or 0)
    test_lines = int(facts.get("test_lines") or 0)
    deviations = [
        d
        for d in (_size_deviation(str(ticket), source_lines, test_lines), _test_ratio_deviation(source_lines, test_lines))
        if d
    ]
    if deviations:
        brief = str(handoff.get("brief") or "")
        handoff["brief"] = "\n".join([brief, *deviations]) if brief else "\n".join(deviations)
    # No patch is an absent artifact as a matter of fact, whatever the node
    # said; with a patch in hand, only the node knows whether an INPUT was
    # missing, so its flag decides.
    blocking = not patch.strip() or bool(handoff.get("blocking"))
    if not handoff.get("complete") and blocking:
        missing = ", ".join(handoff.get("missing") or []) or "unspecified"
        raise ContractViolation(
            f"handoff from build to review is incomplete for '{ticket_id if ticket_id is not None else ticket}': {missing}. "
            "The artifact or an input is missing, so the graph stops rather than "
            "reviewing a change that is not finished — a step that builds on a gap "
            "is how a phase goes quietly wrong."
        )
    return handoff


def _handoff_critique(
    handoff: Mapping[str, Any],
) -> tuple[dict[str, Any], None, None, str, bool, bool]:
    """A non-blocking handoff refusal, in the shape the fix loop already carries.

    The loop moves on a review verdict and a critique, so a handoff that refused
    for missing evidence enters it as exactly that: a `revise` whose findings are
    the handoff's own `missing` list, verbatim. No reviewer ran and none is
    invented — the adversary and the arbitration stay `None`, so nothing
    downstream can mistake the shuttle's objection for a second opinion about
    the code.
    """
    missing = [str(item) for item in handoff.get("missing") or [] if str(item).strip()]
    return (
        {
            "verdict": "revise",
            "findings": [
                {"charter_principle": "handoff evidence", "detail": item, "file": ""}
                for item in missing
            ],
            "rationale": str(handoff.get("brief") or "")
            or f"the handoff refused for missing evidence: {', '.join(missing) or 'unspecified'}",
        },
        None,
        None,
        "revise",
        False,
        False,
    )


def review_is_placeholder(answer: Mapping[str, Any]) -> bool:
    """Pure: does this review answer describe itself instead of judging the change?

    Reads REVIEW_SCHEMA's findings/rationale and ADVERSARY_SCHEMA's
    objections/strongest_objection by the same rule, so the charter and
    adversary reviewers share one check. True when every finding's detail IS
    (not merely contains) one of phase_validate's markers or the bare word
    "placeholder" — a substring search would also catch a real finding ABOUT
    placeholder code, which is the one thing this must never flag. True when
    `rationale` itself is empty, "{}", or only whitespace — REVIEW_SCHEMA
    requires it, so its absence is never legitimate; the check is scoped to
    that field alone, because ADVERSARY_SCHEMA has no `rationale` and an
    adversary that approves with nothing left to object to is not a
    placeholder. True when there are no findings or objections at all and the
    verdict is 'revise' with nothing said either — a revise with nothing
    behind it, on either schema.
    """
    def text(item: Mapping[str, Any]) -> str:
        return str(item.get("detail") or item.get("why_wrong") or item.get("claim") or "").strip().lower()

    items = list(answer.get("findings") or answer.get("objections") or [])
    summary = str(answer.get("rationale") or answer.get("strongest_objection") or "").strip()
    empty_summary = summary in ("", "{}")
    all_placeholders = bool(items) and all(text(item) == "placeholder" or text(item) in _PLACEHOLDER_MARKERS for item in items)
    empty_rationale = "rationale" in answer and str(answer.get("rationale") or "").strip() in ("", "{}")
    empty_revise = not items and answer.get("verdict") == "revise" and empty_summary
    return all_placeholders or empty_rationale or empty_revise


class _NodeFailure(RunnerError):
    """A non-build node raised mid-round; carries the role and whatever the
    round had already decided, so a caller that catches this can quarantine
    with patch and partial verdicts instead of losing both to the traceback.
    """

    def __init__(self, role: str, cause: Exception, *, review: Mapping[str, Any] | None = None,
                 adversary: Mapping[str, Any] | None = None) -> None:
        self.role = role
        self.review = review
        self.adversary = adversary
        super().__init__(f"node '{role}' failed: {cause}")


def _reviewer_answer(
    runner: NodeRunner,
    *,
    role: str,
    hints: Hints | None = None,
    model_tier: str | None = None,  # review_entry still passes a literal tier; its own task drops it.
    schema: Mapping[str, Any],
    context: list[str],
    prompt: str,
    task_id: Any = None,
) -> tuple[dict[str, Any], bool]:
    """One reviewer's answer, and one retry if it is a placeholder instead.

    Same shape as phase_validate._verdict: same role and prompt on retry, plus
    one sentence saying the previous answer did not count. Retried once and
    never more — a reviewer that will not answer twice abstains, and it is the
    caller's to decide what an abstention costs.
    """
    task = None if task_id is None else str(task_id)
    try:
        first = dict(
            runner.run(
                role=role, tier=model_tier, hints=hints, schema=schema, context=context, prompt=prompt, task=task
            )
        )
        if not review_is_placeholder(first):
            return first, False
        second = dict(
            runner.run(
                role=role,
                task=task,
                tier=model_tier,
                hints=hints,
                schema=schema,
                context=context,
                prompt=(
                    f"{prompt}\n\n"
                    "Your previous answer named what you would check instead of checking it — "
                    "a placeholder, not a verdict. Answer for real this time."
                ),
            )
        )
    except BudgetStop:
        raise  # same as `_handoff`: a resumable stop, not a node failure.
    except RunnerError as exc:
        raise _NodeFailure(role, exc) from exc
    return second, review_is_placeholder(second)


def _review_harness_fault() -> dict[str, Any]:
    """Pure: the review a double abstention becomes, never the placeholder verbatim.

    Built from REVIEW_SCHEMA's own keys. Handing the raw placeholder answer
    on as `review` would let its "placeholder" finding read, downstream, as a
    real one — the exact confusion this change exists to stop.
    """
    return {
        "verdict": "revise",
        "findings": [
            {
                "charter_principle": "harness fault",
                "detail": "both reviewers returned a placeholder twice and produced no judgment",
                "file": "",
            }
        ],
        "rationale": "harness fault: review placeholders",
    }


def _patch_truncated_review(reason: str) -> dict[str, Any]:
    """Pure: the review slot for a patch still truncated after its one retry."""
    return {
        "verdict": "revise",
        "findings": [{"charter_principle": "harness fault", "detail": reason, "file": ""}],
        "rationale": "patch_truncated",
    }


def _clean_critique(finding: str) -> dict[str, Any]:
    """Pure: the review slot for a clean-check hit. No reviewer ran; the finding carries the quoted hit lines."""
    return {
        "verdict": "revise",
        "findings": [{"charter_principle": "clean check", "detail": finding, "file": ""}],
        "rationale": "clean_check",
    }


def _clean_gate_row(attempt: int, finding: str | None, status: GateStatus) -> dict[str, str] | None:
    """Pure: the evidence row for one gate outcome; None when there was nothing to gate."""
    if finding is not None:
        return {"check": "clean gate", "output": f"hits found on attempt {attempt}"}
    if status == "skipped":
        return {"check": "clean gate", "output": f"skipped on attempt {attempt}: names pattern not exported (exit 2)"}
    if status == "ran":
        return {"check": "clean gate", "output": f"ran on attempt {attempt}: no hits"}
    return None


def _abstained_review() -> dict[str, Any]:
    """Pure: the charter reviewer's slot in the record once it has abstained.

    Its own words never reach `_critique` or a later build prompt — only the
    fact of the abstention does. Forwarding the raw second answer would put
    the 2026-09-05 text back in front of a builder under a different name.
    """
    return {"verdict": "revise", "findings": [], "rationale": "review_charter did not produce a judgment after a second attempt"}


def _abstained_adversary() -> dict[str, Any]:
    """Pure: the adversary's slot in the record once it has abstained, same rule."""
    return {
        "verdict": "revise",
        "objections": [],
        "strongest_objection": "review_adversary did not produce a judgment after a second attempt",
    }


ARBITER_SKIPPED = "arbiter: skipped (both approved)"


def should_skip_arbiter(charter_verdict: str, adversary_verdict: str) -> bool:
    """True only when both reviewers approved. Revise with revise is over-strict often enough to need the arbiter."""
    return charter_verdict == "approve" and adversary_verdict == "approve"


def review_depth(answer: Answer | None, threshold: float) -> Literal["cheap", "deep"]:
    """cheap only for a confident approve; revise, reject, low confidence and no answer are all deep."""
    confident_approve = answer is not None and answer.value == "approve" and answer.confidence >= threshold
    return "cheap" if confident_approve else "deep"


def _consult_prescreen(runner: NodeRunner, request: Mapping[str, Any]) -> tuple[Literal["cheap", "deep"], str] | None:
    """Edge: the pre-screen's depth and its role's mode, or None when the runner has no pre-screen.

    The depth is computed in shadow and in on alike; `_charter_judgment` decides which one acts on it.
    """
    consult = getattr(runner, "consult", None)
    consulted: Any = consult(str(request.get("role") or "review_charter"), request) if callable(consult) else None
    if consulted is None:
        return None
    answer, setting = consulted
    return review_depth(answer, setting.threshold), setting.mode


def _charter_judgment(prescreen: tuple[Literal["cheap", "deep"], str] | None) -> Literal["low"] | None:
    """Only a live cheap depth lowers the charter reviewer's hints; shadow and deep leave today's call."""
    return "low" if prescreen == ("cheap", "on") else None


def _review_round(
    runner: NodeRunner,
    *,
    context: list[str],
    bound: Mapping[str, Any],
    ticket: Any,
    build: Mapping[str, Any],
    facts: Mapping[str, Any],
    handoff: Mapping[str, Any] | None,
    tier: int,
    attempt: int,
    task_id: Any = None,
    charter_role: str = "review_charter",
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | str | None, str, bool, bool]:
    """One full round of review, and the verdict it reaches.

    Factored out because a retry is reviewed under EXACTLY the same rules as the
    first try — same tier arithmetic, same optional roles, same arbitration
    trigger. A fix loop with a cheaper second pass would be a way of grinding a
    change past its reviewers, which is the thing this loop must not become.

    The fifth element is true when either reviewer placeholdered twice; the
    sixth is true only when BOTH did, which the caller quarantines rather than
    sends back to build — a reviewer that abstains is not a verdict to rebuild
    against.

    `charter_role` is the role that fills the first reviewer's seat: the
    charter reviewer here, the phase reviewer in `phase-review`, which reads a
    phase branch against its base under the same rules.
    """
    # The call declares its role and hints; the resolver picks the model tier.
    patch = str(build.get("patch") or "")
    task = None if task_id is None else str(task_id)
    review_hints = _hints(patch, attempt=attempt)
    arbiter_hints = _hints(patch, attempt=attempt, judgment="high")
    charter_prompt = (
        "Review this change against the team's own written charter in your "
        f"context.\n\nTask: {ticket}\nSummary: {build.get('summary')}\n"
        f"Change facts: {facts}\n"
        + (f"Handoff brief: {handoff.get('brief')}\n" if handoff else "")
        + harness_verify_block(build)
        + f"Patch:\n{build.get('patch')}\n\n"
        "Cite the charter principle behind every finding."
    )
    # The pre-screen lowers only the charter reviewer's hints, and only in mode "on". The adversary
    # keeps today's hints: its job is to disagree with the approve the pre-screen is confident about.
    prescreen = _consult_prescreen(runner, {"role": charter_role, "prompt": charter_prompt})
    review, charter_abstained = _reviewer_answer(
        runner,
        role=charter_role,
        hints=_hints(patch, attempt=attempt, judgment=_charter_judgment(prescreen)),
        schema=REVIEW_SCHEMA,
        context=context,
        prompt=charter_prompt,
        task_id=task_id,
    )

    # Tier 0 is the cheapest review, never the absence of one.
    adversary: dict[str, Any] | None = None
    adversary_abstained = False
    if tier >= 1 and "review_adversary" in bound:
        try:
            adversary, adversary_abstained = _reviewer_answer(
                runner,
                role="review_adversary",
                hints=review_hints,
                schema=ADVERSARY_SCHEMA,
                context=context,
                prompt=(
                    "Your job is to disagree. Find what this change gets wrong, and "
                    "what the first reviewer accepted too easily.\n\n"
                    f"Task: {ticket}\nChange facts: {facts}\n"
                    f"First reviewer said: {review.get('verdict')} — {review.get('rationale')}\n"
                    f"{harness_verify_block(build)}"
                    f"Patch:\n{build.get('patch')}\n\n"
                    "State your strongest objection plainly, even if you end up approving."
                ),
                task_id=task_id,
            )
        except _NodeFailure as exc:
            # The charter reviewer already answered by the time the adversary
            # call raises — attach it, or a mid-round failure here would lose
            # a verdict this same function already has in hand.
            exc.review = dict(review)
            raise

    # An abstained reviewer is dropped from the record and the critique alike —
    # its raw second answer never reaches a builder — and the survivor's
    # verdict decides, through arbitration when the team has bound one, so a
    # tier that would have demanded a third read still gets one. Neither
    # survives, and there is nothing to decide with — the caller quarantines
    # that.
    try:
        charter_usable, adversary_usable = not charter_abstained, adversary is not None and not adversary_abstained
        if not charter_usable and not adversary_usable:
            return _review_harness_fault(), adversary, None, "revise", True, True
        if not charter_usable:
            assert adversary is not None  # adversary_usable: the check above returned when neither was
            sole_arbitration = None
            if "arbitrate" in bound:
                sole_arbitration = dict(
                    runner.run(
                        role="arbitrate",
                        task=task,
                        hints=arbiter_hints,
                        schema=ARBITRATE_SCHEMA,
                        context=context,
                        prompt=(
                            "One reviewer has looked at this change; the other did not produce a "
                            "judgment after a second attempt and has abstained. Decide.\n\n"
                            f"Task: {ticket}\nReview tier: {tier}\n"
                            "Charter reviewer: abstained, no judgment\n"
                            f"Adversary: {adversary.get('verdict')} — {adversary.get('strongest_objection')}\n"
                            f"Change facts: {facts}\n\n"
                            "Say who you sided with and why. 'neither' is allowed."
                        ),
                    )
                )
            sole_verdict = str(sole_arbitration.get("verdict")) if sole_arbitration is not None else str(adversary.get("verdict"))
            return _abstained_review(), adversary, sole_arbitration, sole_verdict, True, False
        if not adversary_usable and adversary is not None:
            sole_arbitration = None
            if "arbitrate" in bound:
                sole_arbitration = dict(
                    runner.run(
                        role="arbitrate",
                        task=task,
                        hints=arbiter_hints,
                        schema=ARBITRATE_SCHEMA,
                        context=context,
                        prompt=(
                            "One reviewer has looked at this change; the other did not produce a "
                            "judgment after a second attempt and has abstained. Decide.\n\n"
                            f"Task: {ticket}\nReview tier: {tier}\n"
                            f"Charter reviewer: {review.get('verdict')} — {review.get('rationale')}\n"
                            "Adversary: abstained, no judgment\n"
                            f"Change facts: {facts}\n\n"
                            "Say who you sided with and why. 'neither' is allowed."
                        ),
                    )
                )
            sole_verdict = str(sole_arbitration.get("verdict")) if sole_arbitration is not None else str(review.get("verdict"))
            return dict(review), _abstained_adversary(), sole_arbitration, sole_verdict, True, False

        # Arbitration on disagreement, and at tier 2 unless both approved. Measured
        # over 383 records, approve with approve was upheld 83 of 83; revise with
        # revise was overturned 12 of 82, so only the approving pair skips.
        arbitration: dict[str, Any] | str | None = None
        disagreed = adversary is not None and adversary.get("verdict") != review.get("verdict")
        skip = adversary is not None and should_skip_arbiter(str(review.get("verdict")), str(adversary.get("verdict")))
        if "arbitrate" in bound and skip and tier == 2:
            arbitration = ARBITER_SKIPPED
        elif "arbitrate" in bound and adversary is not None and (disagreed or tier == 2):
            arbitration = dict(
                runner.run(
                    role="arbitrate",
                    task=task,
                    hints=arbiter_hints,
                    schema=ARBITRATE_SCHEMA,
                    context=context,
                    prompt=(
                        "Two reviewers have looked at this change. Decide.\n\n"
                        f"Task: {ticket}\nReview tier: {tier}\n"
                        f"Charter reviewer: {review.get('verdict')} — {review.get('rationale')}\n"
                        f"Adversary: {adversary.get('verdict')} — {adversary.get('strongest_objection')}\n"
                        f"Change facts: {facts}\n\n"
                        "Say who you sided with and why. 'neither' is allowed."
                    ),
                )
            )
    except BudgetStop:
        raise  # same as `_handoff`: a resumable stop, not a node failure.
    except RunnerError as exc:
        raise _NodeFailure("arbitrate", exc, review=dict(review), adversary=adversary) from exc

    # The last word: arbitration if it ran, otherwise both reviewers must agree.
    # Silence from an unbound optional role is not an approval, but neither is it
    # an objection — an unbound adversary simply leaves the charter reviewer
    # deciding, exactly as before.
    if isinstance(arbitration, Mapping):
        verdict = str(arbitration.get("verdict"))
    elif arbitration == ARBITER_SKIPPED:
        verdict = "approve"
    elif adversary is not None:
        verdict = "approve" if review.get("verdict") == adversary.get("verdict") == "approve" else "revise"
    else:
        verdict = str(review.get("verdict"))

    return dict(review), adversary, arbitration, verdict, False, False
