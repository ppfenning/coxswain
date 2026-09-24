"""The model router's decision: compose the step rules, then clip to the chair's bounds.

`decide` returns a `Decision`, or a list of error strings when no honest decision exists:
pacing `stop`, an exhausted run budget, a ceiling the router cannot enforce, no usable
catalog model, or no model between the floor and a ceiling. `observed_costs` and
`spend_usd` are required, so a caller cannot leave the run cap silently off.
`clipped_by` is in application order. "pacing" is named only when the chair's go_degraded
state, applied to the hints, changed class or effort.
unknown: bounds.py class ceilings use TIER_LADDER (deep, standard, cheap), not the catalog
classes. The top tier `deep` means no ceiling. Any other tier is refused until reconciled.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType

from core.bounds import ChairBounds
from core.catalog import Catalog, ModelEntry
from core.policy import TIER_LADDER
from core.router_budget import budget_usd
from core.router_rules import DEGRADED, NEVER_BELOW_FLOOR, Hints, History, floor_class, select_class, select_effort

# The constants below are placeholders to be measured; decide's signature has no slot for them.
CLASS_LADDER = ("extract", "reason", "judge", "frontier")
EFFORT_LADDER = ("low", "medium", "high", "xhigh")
FLOORS = MappingProxyType({"build": "reason", "arbitrate": "judge"})
REVIEWED = frozenset({"review"})
BASE_EFFORT = "medium"
DIFF_THRESHOLD = 400
FILE_THRESHOLD = 10
STOP = "stop"
TOP_TIER = TIER_LADDER[0]
_RULE_KW = {"reviewed": REVIEWED, "diff_threshold": DIFF_THRESHOLD, "file_threshold": FILE_THRESHOLD}


@dataclass(frozen=True)
class Decision:
    model: str
    effort: str
    budget_usd: float
    reasons: tuple[str, ...]
    clipped_by: tuple[str, ...]


def _price(entry: ModelEntry) -> float:
    return entry.price.input + entry.price.output


def _cheapest(entries: Sequence[ModelEntry]) -> ModelEntry:
    return min(entries, key=lambda e: (_price(e), e.id))


def _within(ladder: tuple[str, ...], value: str, ceiling: str) -> bool:
    return ceiling not in ladder or ladder.index(value) <= ladder.index(ceiling)


def _nearest(ladder: tuple[str, ...], wanted: str, pool: set[str]) -> str:
    """The rung in `pool` closest to `wanted`; a tie goes to the lower rung."""
    return min(pool, key=lambda r: (abs(ladder.index(r) - ladder.index(wanted)), ladder.index(r)))


def _clip(ladder: tuple[str, ...], value: str, ceiling: str) -> tuple[str, bool]:
    return (value, False) if _within(ladder, value, ceiling) else (ceiling, True)


def _ceiling_errors(bounds: ChairBounds) -> list[str]:
    """A ceiling off its ladder cannot be enforced. The top policy tier means no class ceiling."""
    checks = (
        ("class", CLASS_LADDER, bounds.class_ceiling, TOP_TIER),
        ("effort", EFFORT_LADDER, bounds.effort_ceiling, None),
    )
    return [
        f"{kind} ceiling {ceiling} is not on the {kind} ladder {list(ladder)} and cannot be enforced"
        for kind, ladder, ceiling, open_value in checks
        if ceiling not in ladder and ceiling != open_value
    ]


def _fired(label: str, reason: str) -> tuple[str, ...]:
    return tuple(f"{label}: {r}" for r in reason.split("; ") if r != "no rule fired")


def _usable(catalog: Catalog) -> tuple[ModelEntry, ...]:
    """Entries with at least one class and one effort the router's ladders know."""
    return tuple(
        e
        for e in catalog.entries
        if any(c in CLASS_LADDER for c in e.classes) and any(x in EFFORT_LADDER for x in e.effort)
    )


def _pick(
    entries: tuple[ModelEntry, ...], cls: str, effort: str, lowest: str, class_ceiling: str, effort_ceiling: str
) -> tuple[ModelEntry, str, tuple[str, ...]] | str:
    """Cheapest carrier of `cls` supporting `effort`; else the nearest class in [lowest, ceiling] and effort.

    Returns an error string when no model fits between the bounds.
    """
    used, notes = cls, ()
    carrying = tuple(e for e in entries if cls in e.classes)
    if not carrying:
        pool = {
            c
            for e in entries
            for c in e.classes
            if c in CLASS_LADDER
            and CLASS_LADDER.index(c) >= CLASS_LADDER.index(lowest)
            and _within(CLASS_LADDER, c, class_ceiling)
        }
        if not pool:
            return f"no model carries a class between {lowest} and ceiling {class_ceiling}"
        used = _nearest(CLASS_LADDER, cls, pool)
        carrying = tuple(e for e in entries if used in e.classes)
        notes = (f"no model carries class {cls}; using class {used}",)
    exact = tuple(e for e in carrying if effort in e.effort)
    if exact:
        return _cheapest(exact), effort, notes
    allowed = {
        x for e in carrying for x in e.effort if x in EFFORT_LADDER and _within(EFFORT_LADDER, x, effort_ceiling)
    }
    if not allowed:
        return f"no model carrying class {used} supports an effort at or below ceiling {effort_ceiling}"
    chosen = _nearest(EFFORT_LADDER, effort, allowed)
    entry = _cheapest(tuple(e for e in carrying if chosen in e.effort))
    return entry, chosen, (*notes, f"effort {effort} unsupported for class {used}; using {chosen}")


def _reference_costs(
    observed: Sequence[tuple[str, float]], catalog: Catalog, reference: ModelEntry
) -> tuple[tuple[float, ...], tuple[str, ...]]:
    """Restate each observed cost at `reference`'s price, so the budget rule's ratio applies to one basis."""
    prices = {e.id: _price(e) for e in catalog.entries}
    costs = tuple(cost * _price(reference) / prices[m] for m, cost in observed if m in prices)
    return costs, tuple(f"ignored observed turn cost for unknown model {m}" for m, _ in observed if m not in prices)


def _clip_budget(
    budget: float, node_cap: float | None, run_cap: float | None, spend: float
) -> tuple[float, str | None]:
    remaining = None if run_cap is None else run_cap - spend
    caps = [(v, n) for v, n in ((node_cap, "node_budget_cap"), (remaining, "run_budget_cap")) if v is not None]
    if not caps:
        return budget, None
    lowest, name = min(caps, key=lambda c: c[0])
    return (lowest, name) if budget > lowest else (budget, None)


def decide(
    role: str,
    hints: Hints,
    history: History,
    bounds: ChairBounds,
    catalog: Catalog,
    *,
    observed_costs: Sequence[tuple[str, float]],
    spend_usd: float,
) -> Decision | list[str]:
    if STOP in (bounds.pacing_state, hints.pacing_state):
        return [f"pacing state is {STOP}: no model to route"]
    run_cap = bounds.run_budget_cap_usd
    if run_cap is not None and spend_usd >= run_cap:
        return [f"run budget exhausted: spent ${spend_usd:.2f} of ${run_cap:.2f}"]
    if errors := _ceiling_errors(bounds):
        return errors
    entries = _usable(catalog)
    if not entries:
        return ["catalog has no usable model: needs a class and an effort the router's ladders know"]

    def rules(h: Hints) -> tuple[str, str, str, str]:
        cls, cls_reason = select_class(role, CLASS_LADDER, FLOORS, h, history, **_RULE_KW)
        effort, effort_reason = select_effort(role, EFFORT_LADDER, BASE_EFFORT, h, history, **_RULE_KW)
        return cls, effort, cls_reason, effort_reason

    paced_hints = replace(hints, pacing_state=DEGRADED) if bounds.pacing_state == DEGRADED else hints
    cls, effort, cls_reason, effort_reason = rules(paced_hints)
    paced = rules(hints)[:2] != (cls, effort)
    cls_to, cls_clipped = _clip(CLASS_LADDER, cls, bounds.class_ceiling)
    effort_to, effort_clipped = _clip(EFFORT_LADDER, effort, bounds.effort_ceiling)
    floor = floor_class(role, CLASS_LADDER, FLOORS)[0]
    never_below = role in NEVER_BELOW_FLOOR
    forced = cls_clipped and never_below and CLASS_LADDER.index(cls_to) < CLASS_LADDER.index(floor)
    lowest = min(floor, cls_to, key=CLASS_LADDER.index) if never_below else CLASS_LADDER[0]
    picked = _pick(entries, cls_to, effort_to, lowest, bounds.class_ceiling, bounds.effort_ceiling)
    if isinstance(picked, str):
        return [picked]
    entry, final_effort, pick_notes = picked
    reference = _cheapest(entries)
    costs, cost_notes = _reference_costs(observed_costs, catalog, reference)
    budget, budget_reason = budget_usd(costs, price_ratio=_price(entry) / _price(reference))
    capped, cap_name = _clip_budget(budget, bounds.node_budget_cap_usd, run_cap, spend_usd)
    reasons = (
        *_fired("class", cls_reason),
        *_fired("effort", effort_reason),
        *((f"class {cls} clipped to ceiling {cls_to}",) if cls_clipped else ()),
        *((f"{role} forced below floor by class_ceiling",) if forced else ()),
        *((f"effort {effort} clipped to ceiling {effort_to}",) if effort_clipped else ()),
        *pick_notes,
        *cost_notes,
        f"budget: {budget_reason}",
        *((f"budget ${budget:.2f} clipped to ${capped:.2f} by {cap_name}",) if cap_name else ()),
    )
    named = (
        ("pacing", paced),
        ("class_ceiling", cls_clipped),
        ("effort_ceiling", effort_clipped),
        (cap_name, cap_name is not None),
    )
    return Decision(entry.id, final_effort, capped, reasons, tuple(name for name, hit in named if hit))
