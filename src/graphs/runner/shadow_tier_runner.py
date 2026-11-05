"""ShadowTierRunner: one extra call at a cheap tier beside each call of a configured role.

The shadow call exists only to be compared. Its result is never returned and never read by a caller.
Its verdict, cost and model are written onto the acting call's decision through the inner
runner's `tag_decision` hook, so they land before the ledger write.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from runner.decision_log import CallDecision, RouterDecision
from runner.protocol import NodeResult, NodeRunner
from runner.shadow_tier import ShadowConfig, compare, shadow_verdict
from runner.tier_resolution import Hints

_log = logging.getLogger(__name__)

TagHook = Callable[[NodeResult], CallDecision | None]


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def shadow_fields(
    role: str,
    tier: str,
    acting: Mapping[str, object],
    shadow: Mapping[str, object],
    model_id: str | None,
    cost_usd: float | None,
) -> dict[str, Any]:
    """The shadow_* fields of one acting call's decision. A verdict neither side can read stays None."""
    verdict = compare(role, acting, shadow)
    return {
        "shadow_tier": tier,
        "shadow_model_id": model_id,
        "shadow_verdict": shadow_verdict(role, shadow),
        "shadow_agreed": verdict.agreed,
        "shadow_costly": verdict.costly,
        "shadow_cost_usd": cost_usd,
    }


def shadow_facts(result: NodeResult, new_calls: Sequence[Mapping[str, Any]] | None) -> tuple[str | None, float | None]:
    """(model id, cost) of the shadow call. The model comes from its decision or its call record.

    The cost comes from the call record only, and only when exactly one record is new, so a
    concurrent call of the same role cannot lend its cost. None when it cannot be told.
    """
    decision = getattr(result, "decision", None)
    record = new_calls[0] if new_calls is not None and len(new_calls) == 1 else None
    model = getattr(decision, "model_id", None) or (record.get("model") if record is not None else None)
    return (model if isinstance(model, str) and model else None), (
        _number(record.get("cost_usd")) if record is not None else None
    )


def _chain(own: TagHook | None, outer: TagHook | None) -> TagHook | None:
    """One hook that applies `own`, then hands its decision to `outer`, so both tags land."""
    if own is None or outer is None:
        return own or outer

    def hook(result: NodeResult) -> CallDecision | None:
        mine = own(result)
        probe = NodeResult(result)
        probe.decision = mine if mine is not None else getattr(result, "decision", None)
        theirs = outer(probe)
        return theirs if theirs is not None else mine

    return hook


class ShadowTierRunner:
    """A NodeRunner that adds one shadow call at the configured tier for each call of a configured role.

    It holds no per-call state: the hook for a call lives in that call's frame, and the outer
    hook lives in a thread-local, because one wrapper is shared across a thread pool. Public
    attributes read and write through to the inner runner. Underscored names stay here.
    """

    def __init__(self, inner: NodeRunner, config: ShadowConfig) -> None:
        self._inner = inner
        self._tiers = dict(config.tiers)
        # Holds the hook an outer FastPathRunner sets on this wrapper, per thread.
        self._local = threading.local()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_") or name == "tag_decision":
            object.__setattr__(self, name, value)
        else:
            setattr(self._inner, name, value)

    @property
    def tag_decision(self) -> TagHook | None:
        """The calling thread's outer hook. `run` applies it to the acting call, never to the shadow call."""
        return getattr(self._local, "tag_decision", None)

    @tag_decision.setter
    def tag_decision(self, hook: TagHook | None) -> None:
        self._local.tag_decision = hook

    def _shadow_call(
        self, kwargs: Mapping[str, Any], tier: str
    ) -> tuple[NodeResult, Sequence[Mapping[str, Any]] | None] | None:
        """One inner call at the shadow tier with no hints, thread or hook. None when it raises."""
        calls = getattr(self._inner, "calls", None)
        before = len(calls) if isinstance(calls, list) else None
        shadow_kwargs = {k: kwargs[k] for k in ("role", "schema", "prompt", "context", "task")}
        try:
            result = self._inner.run(**shadow_kwargs, tier=tier, hints=None, thread=None)
        except Exception as exc:
            _log.warning(
                "shadow_tier: the %s shadow call at %s failed, the acting call is recorded without it: %s",
                kwargs["role"],
                tier,
                exc,
            )
            return None
        return result, (calls[before:] if before is not None else None)

    def _own_hook(
        self, role: str, tier: str, shadow: NodeResult, new_calls: Sequence[Mapping[str, Any]] | None
    ) -> TagHook:
        model_id, cost_usd = shadow_facts(shadow, new_calls)

        def hook(result: NodeResult) -> CallDecision | None:
            decision = getattr(result, "decision", None)
            if decision is None:
                return None
            return replace(decision, **shadow_fields(role, tier, result, shadow, model_id, cost_usd))

        return hook

    def _act(self, kwargs: Mapping[str, Any], own: TagHook | None) -> NodeResult:
        hook = _chain(own, self.tag_decision)
        if hook is None:
            return self._inner.run(**kwargs)
        if hasattr(self._inner, "tag_decision"):
            self._inner.tag_decision = hook
            try:
                return self._inner.run(**kwargs)
            finally:
                self._inner.tag_decision = None
        result = self._inner.run(**kwargs)
        tagged = hook(result)
        if tagged is not None:
            # Edge case, said here: with no inner hook the decision is replaced on the acting
            # result itself, so the returned object stays the acting result.
            result.decision = tagged
        return result

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
        wait_if_paused: Callable[[], None] | None = None,
    ) -> NodeResult:
        kwargs: dict[str, Any] = {
            "role": role,
            "tier": tier,
            "hints": hints,
            "schema": schema,
            "prompt": prompt,
            "context": context,
            "thread": thread,
            "budget_usd": budget_usd,
            "task": task,
        }
        # The inner runner gets these only when given, so an inner runner that predates them still runs.
        extras = {"router_decision": router_decision, "wait_if_paused": wait_if_paused}
        passed = {**kwargs, **{k: v for k, v in extras.items() if v is not None}}
        shadow_tier = self._tiers.get(role)
        if shadow_tier is None:
            return self._act(passed, None)
        shadowed = self._shadow_call(kwargs, shadow_tier)
        own = None if shadowed is None else self._own_hook(role, shadow_tier, *shadowed)
        return self._act(passed, own)
