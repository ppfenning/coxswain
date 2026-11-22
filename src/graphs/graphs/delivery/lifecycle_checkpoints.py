"""lifecycle-propose's checkpoints and tiers.

What happens on a budget stop (continue the session, or send the build back), the checkpoint call that decides it,
and the tier each call asks for: a ticket's `tier:` map, the one escalation, and the decision-source hook.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, NamedTuple
from uuid import uuid4

from core.bounded_proc import GIT_TIMEOUT_SECONDS, completed_in_group
from graphs.delivery.checkpoint import (
    CheckpointSignals,
    Decision,
    Revise,
    checkpoint_decision,
    checkpoint_ledger_line,
)
from graphs.delivery.lifecycle_patch import _drop_scratch, _files_outside_surfaces, _scratch_ignored, _touched_paths
from graphs.delivery.lifecycle_schemas import BUILD_SCHEMA
from runner.decision_log import RouterDecision
from runner.decision_source import DecisionSource, ask
from runner.protocol import BudgetStop, NodeResult, NodeRunner
from runner.tier_resolution import TIERS, Hints, rank

# A budget stop is not a failure to discard; the worktree holds the partial
# patch and the session holds the context. Two continuations, not unbounded —
# past this the task is too large for the slice it was given, and that is a
# scoping problem, not a reason to keep burning budget on the same session.
CONTINUATIONS_MAX = 2


# The table `checkpoint_decision` judges a checkpoint's position against:
# `ClaudeCodeRunner._shape_checkpoint`'s default. It is NOT always the
# runner's table. Once a role's bounds row is proven (`n >= 20`) the runner
# uses that row's fractions, and a row of another length moves the "last
# checkpoint" this graph sees. `BudgetStop` carries no fractions to read back.
CHECKPOINT_FRACTIONS = (1.0, 2.0)


def _is_budget_stop(exc: Exception) -> bool:
    """Whether a `RunnerError` is the CLI's dollar-ceiling stop, not some other failure."""
    return "error_max_budget_usd" in str(exc).lower()


def _continue_ok(stop: BudgetStop, *, surfaces: list[str], continuations: int,
                 hard_ceiling: float | None = None) -> tuple[bool, str]:
    """Whether a budget-stopped build is worth resuming, and why not when it isn't.

    A fact-only check — no model is asked whether to continue, so it costs
    nothing to run on every stop. The three no-go reasons are different
    signals and each names its own remedy: whoever reads the quarantine acts
    on the text, not on a bare "no".
    """
    if not stop.session:
        return False, "no session to resume"

    touched = _touched_paths(stop.partial_patch or "")
    outside = _files_outside_surfaces(touched, surfaces)
    if outside:
        return False, (
            f"partial work touches {outside[0]} outside the task's surfaces: "
            "re-scope the task"
        )

    if not (stop.partial_patch or "").strip():
        if continuations == 0:
            return True, ""
        return False, (
            "two budget slices produced no change: set budget_usd on the work "
            "item if the task is legitimately this large, otherwise the task "
            "body is the problem"
        )

    untouched = [surface for surface in surfaces if surface not in touched]
    if hard_ceiling is not None:
        # A role with a runaway ceiling resumes at every checkpoint that passes the checks above,
        # as many times as it takes, until its session's cumulative spend reaches the failsafe.
        if (stop.spent_usd or 0.0) < hard_ceiling:
            return True, ""
        return False, (
            f"the ${hard_ceiling:.2f} runaway ceiling was reached with partial work: split recommended — "
            f"done: {', '.join(touched)}; untouched: {', '.join(untouched)}"
        )

    if continuations < CONTINUATIONS_MAX:
        return True, ""

    return False, (
        "continuation cap reached with partial work: split recommended — "
        f"done: {', '.join(touched)}; untouched: {', '.join(untouched)}"
    )


class _CheckpointContext(NamedTuple):
    """What a checkpoint needs beyond the stop, read once from the unwrapped runner.

    `repo_dir` is the checkout the build session's scratch was cut from
    (`ClaudeCodeRunner.repo_dir`, which the epic driver points at the phase
    worktree), so the partial patch is a diff against its `HEAD`. The two
    budget maps are the profile half of `ClaudeCodeRunner._shape_ceiling`.
    """

    checks: tuple[Mapping[str, Any], ...]
    repo_dir: str | None
    role_budget_usd: Mapping[str, float]
    tier_budget_usd: Mapping[str, float]
    runs_dir: Path | None
    run_id: str
    task: str


def _checkpoint_context(cartridge: Mapping[str, Any], raw_runner: Any, run_id: str, ticket: Any) -> _CheckpointContext:
    """The ticket's named checks and the runner facts a checkpoint reads.

    Read off the raw runner, not the `_Elevated` wrapper, which forwards no
    attributes: a `getattr` through it finds nothing. `runs_dir` is the same
    attribute `ClaudeCodeRunner` kept for the old per-call `calls.jsonl`
    ledger; nothing reads that ledger any more, so this is its only reader now.
    """
    repo_dir = getattr(raw_runner, "repo_dir", None)
    runs_dir = getattr(raw_runner, "runs_dir", None)
    return _CheckpointContext(
        checks=tuple((cartridge.get("landing_areas") or {}).get("checks") or ()),
        repo_dir=str(repo_dir) if repo_dir else None,
        role_budget_usd=dict(getattr(raw_runner, "role_budget_usd", None) or {}),
        tier_budget_usd=dict(getattr(raw_runner, "budget_usd", None) or {}),
        runs_dir=Path(runs_dir) if runs_dir else None,
        run_id=run_id,
        task=str(ticket),
    )


def _guide_usd(ctx: _CheckpointContext, budget_usd: float | None, tier: str) -> float | None:
    """The per-slice guide the checkpoint fractions multiply, not the runaway ceiling.

    The profile branch of `ClaudeCodeRunner._shape_ceiling`: a call's own
    budget, else the role's, else the tier's. A proven bounds row can replace
    it inside the runner, keyed by a model this graph never sees.
    """
    if budget_usd is not None:
        return budget_usd
    role = ctx.role_budget_usd.get("build")
    return role if role is not None else ctx.tier_budget_usd.get(tier)


def _checkpoint_checks_pass(patch: str, checks: Sequence[Mapping[str, Any]], repo_dir: str | None) -> bool:
    """The ticket's named checks, run in a worktree of `repo_dir` at `HEAD` holding `patch`.

    A real checkout, as `review_entry._verify_evidence` and the epic driver's
    task step make one: `create_worktree`, `link_venv`, `apply_patch`, then
    `run_checks`. A bare `apply_patch` scratch has no project behind it, so a
    patch editing an existing file will not even apply there. With no checks,
    or no repository to check out, there is nothing that can fail: the split
    rule needs failing checks, not unmeasured ones. A patch that does not
    apply to `HEAD` fails. The worktree and its branch are removed on the way out.

    Imported here, not at module level: `harness` imports this module (via
    `harness.epic` -> `graphs.delivery.rescue_review`), so a top-level
    `from harness.checks import ...` here is circular.
    """
    if not checks or not repo_dir:
        return True
    from harness.checks import all_passed, run_checks
    from harness.worktree import apply_patch, create_worktree, link_venv, remove_worktree

    repo = Path(repo_dir)
    branch = f"checkpoint-{uuid4().hex[:8]}"
    with TemporaryDirectory() as scratch:
        worktree = Path(scratch) / "worktree"
        made, _detail = create_worktree(repo, worktree, branch=branch, base="HEAD")
        if not made:
            return True
        try:
            link_venv(repo, worktree)
            applied, _detail = apply_patch(patch or "", worktree)
            return applied and all_passed(run_checks(worktree, checks))
        finally:
            remove_worktree(repo, worktree)
            completed_in_group(["git", "-C", str(repo), "branch", "-D", branch], timeout=GIT_TIMEOUT_SECONDS, capture_output=True, text=True)


def _append_checkpoint_ledger(ctx: _CheckpointContext, signals: CheckpointSignals, decision: Decision) -> None:
    """Append one ledger line for this checkpoint to `<runs_dir>/<run_id>.checkpoints.jsonl`.

    A no-op when the context carries no `runs_dir` — a caller that never set
    one (most tests) gets no file rather than a path built from `None`. The
    file is opened in append mode only: this writer never rewrites what is
    already on disk. A reader lives in `cox stats`, in src/tools.
    """
    if ctx.runs_dir is None:
        return
    line = checkpoint_ledger_line(
        ts=datetime.now(UTC).isoformat(), task=ctx.task, role="build", signals=signals, decision=decision
    )
    path = ctx.runs_dir / f"{ctx.run_id}.checkpoints.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def _checkpoint_call(
    stop: BudgetStop,
    *,
    surfaces: list[str],
    ctx: _CheckpointContext,
    guide_usd: float | None,
    previous_patch: str | None,
) -> Decision:
    """The `CheckpointSignals` a checkpoint stop carries, judged by `checkpoint_decision`.

    `diff_grew` compares this stop's patch to the one recorded at the
    previous checkpoint on the same thread; a first checkpoint has nothing to
    compare against, so it reads as grown. The rules themselves live in
    `graphs/delivery/checkpoint.py` and are not repeated here. Every decision
    reached, Resume or Revise, is appended to the run's checkpoint ledger.
    """
    touched = _touched_paths(stop.partial_patch or "")
    outside = _files_outside_surfaces(touched, surfaces)
    scratch = _scratch_ignored(outside, ctx.repo_dir)
    checked_patch = _drop_scratch(stop.partial_patch or "", scratch)
    signals = CheckpointSignals(
        spend_usd=stop.spent_usd or 0.0,
        guide_usd=guide_usd or 0.0,
        checkpoint_index=stop.checkpoint_index or 0,
        turns=stop.num_turns or 0,
        diff_grew=previous_patch is None or stop.partial_patch != previous_patch,
        checks_pass=_checkpoint_checks_pass(checked_patch, ctx.checks, ctx.repo_dir),
        files_outside_surfaces=tuple(f for f in outside if f not in scratch),
        scratch_ignored=tuple(scratch),
    )
    decision = checkpoint_decision(signals, CHECKPOINT_FRACTIONS)
    _append_checkpoint_ledger(ctx, signals, decision)
    return decision


def _tier_for(role: str, literal: str | None, ticket_tiers: Mapping[str, str]) -> str | None:
    """The tier a call asks for: the ticket's, only when it is higher than the literal.

    A ticket never lowers a literal. With no literal the ticket's tier is
    named outright, which beats the profile default in the runner's resolver.
    An unknown tier name raises ValueError from `rank`.
    """
    mapped = ticket_tiers.get(role)
    if mapped is None:
        return literal
    if literal is None:
        return mapped
    return mapped if rank(mapped) > rank(literal) else literal


def _escalate(tier: str) -> str:
    """One tier up; the top tier stays where it is."""
    return TIERS[min(rank(tier) + 1, len(TIERS) - 1)]


class _Elevated:
    """A runner whose every call carries the ticket's per-role tier, via `_tier_for`.

    `run` takes exactly the protocol's parameters and no `**kwargs`: a runner
    accepts nothing else, so anything extra would be a TypeError on a live call.
    """

    def __init__(self, inner: NodeRunner, ticket_tiers: Mapping[str, str]) -> None:
        self._inner = inner
        self._ticket_tiers = dict(ticket_tiers)

    def run(
        self,
        *,
        role: str,
        tier: str | None = None,
        hints: Hints | None = None,
        schema: Mapping[str, Any],
        prompt: str,
        context: Sequence[str] = (),
        thread: str | None = None,
        budget_usd: float | None = None,
        task: str | None = None,
    ) -> NodeResult:
        return self._inner.run(
            role=role,
            tier=_tier_for(role, tier, self._ticket_tiers),
            hints=hints,
            schema=schema,
            prompt=prompt,
            context=context,
            thread=thread,
            budget_usd=budget_usd,
            task=task,
        )


class _Asking:
    """A runner whose every call carries the source's decision for its role and hints.

    The decision is a shadow value: `tier` and every other argument pass through
    untouched. A None decision is left off the call, so a runner written before
    `router_decision` existed still accepts it.
    """

    def __init__(self, inner: NodeRunner, source: DecisionSource) -> None:
        self._inner = inner
        self._source = source

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def run(
        self,
        *,
        role: str,
        tier: str | None = None,
        hints: Hints | None = None,
        schema: Mapping[str, Any],
        prompt: str,
        context: Sequence[str] = (),
        thread: str | None = None,
        budget_usd: float | None = None,
        task: str | None = None,
        router_decision: RouterDecision | None = None,
    ) -> NodeResult:
        decision = ask(self._source, role, hints)
        extra: dict[str, Any] = {"router_decision": decision} if decision is not None else {}
        return self._inner.run(
            role=role,
            tier=tier,
            hints=hints,
            schema=schema,
            prompt=prompt,
            context=context,
            thread=thread,
            budget_usd=budget_usd,
            task=task,
            **extra,
        )


def _resume_build(
    runner: NodeRunner,
    *,
    context: list[str],
    ticket: Any,
    budget_usd: float | None,
    surfaces: list[str],
    stop: BudgetStop,
    continuations: int,
    checkpoint: _CheckpointContext | None = None,
    previous_checkpoint_patch: str | None = None,
    tier: str = "standard",
) -> tuple[dict[str, Any] | None, int, str, BudgetStop, str | None, str | None]:
    """Decide go/no-go on a budget stop and, on go, resume until one finishes
    or the cap refuses another.

    A stop with `checkpoint_index` set is judged by `checkpoint_decision`
    instead of `_continue_ok`, whatever the continuation count or role
    ceiling — a checkpoint `Revise` is a no-go regardless of both. A stop
    with no `checkpoint_index` (the final-ceiling stop) goes through
    `_continue_ok` exactly as before.

    Returns the finished build (`None` on no-go), the updated continuation
    count, the no-go reason (empty on go), the last `BudgetStop` seen, the
    previous-checkpoint patch updated for the next stop on this thread, and a
    checkpoint revise reason — set only when the no-go came from a
    checkpoint's own `Revise`, so the caller knows not to raise.
    """
    ctx = checkpoint if checkpoint is not None else _checkpoint_context({}, None, "", ticket)
    while True:
        hard = (getattr(runner, "role_ceiling_usd", None) or {}).get("build")
        if stop.checkpoint_index is not None:
            decision = _checkpoint_call(
                stop, surfaces=surfaces, ctx=ctx, guide_usd=_guide_usd(ctx, budget_usd, tier),
                previous_patch=previous_checkpoint_patch,
            )
            if isinstance(decision, Revise):
                return None, continuations, "", stop, previous_checkpoint_patch, decision.reason
            previous_checkpoint_patch = stop.partial_patch
        else:
            go, reason = _continue_ok(stop, surfaces=surfaces, continuations=continuations, hard_ceiling=hard)
            if not go:
                return None, continuations, reason, stop, previous_checkpoint_patch, None
        try:
            build = runner.run(
                role="build",
                tier=tier,
                thread=str(ticket),
                task=str(ticket),
                schema=BUILD_SCHEMA,
                context=context,
                budget_usd=budget_usd,
                prompt=(
                    "Your previous session stopped at its budget ceiling. Nothing "
                    "you did is lost: the worktree holds your partial change and "
                    "this session holds your context. Continue from exactly where "
                    "you stopped — do not start over and do not re-read what you "
                    "already read. Finish the change, run the checks, and return "
                    "the unified diff of the WHOLE change."
                ),
            )
        except BudgetStop as exc:
            continuations += 1
            stop = exc
            continue
        continuations += 1
        return dict(build), continuations, "", stop, previous_checkpoint_patch, None
