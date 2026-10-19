"""The wire form of a router `Decision`: a plain JSON-safe dict a caller hands to graphs.

Graphs never imports cartridges, so it mirrors this shape in its own type. The keys:

    schema       integer, always 1. Any other number is refused.
    chosen_class string, the capability class the router picked.
    model        string, the catalog model id.
    effort       string, the reasoning effort.
    budget_usd   number, the dollar budget for the step.
    reasons      list of strings, the rules that fired, in application order.
    clipped_by   list of strings, the bounds that clipped the choice, in application order.

`decision_from_wire` returns a `Decision`, or a list of error strings. It checks shape only.
"""

from __future__ import annotations

from core.router import Decision

SCHEMA = 1
_STRINGS = ("chosen_class", "model", "effort")
_LISTS = ("reasons", "clipped_by")
_KEYS = ("schema", *_STRINGS, "budget_usd", *_LISTS)


def decision_to_wire(decision: Decision) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "chosen_class": decision.chosen_class,
        "model": decision.model,
        "effort": decision.effort,
        "budget_usd": decision.budget_usd,
        "reasons": list(decision.reasons),
        "clipped_by": list(decision.clipped_by),
    }


def _is_number(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _is_str_list(v: object) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _errors(wire: dict[str, object]) -> list[str]:
    missing = [f"missing key: {k}" for k in _KEYS if k not in wire]
    schema = wire.get("schema", SCHEMA)
    bad_schema = [] if schema == SCHEMA and not isinstance(schema, bool) else [f"unknown schema: {schema!r}"]
    bad_strings = [f"{k} must be a string" for k in _STRINGS if k in wire and not isinstance(wire[k], str)]
    bad_budget = ["budget_usd must be a number"] if "budget_usd" in wire and not _is_number(wire["budget_usd"]) else []
    bad_lists = [f"{k} must be a list of strings" for k in _LISTS if k in wire and not _is_str_list(wire[k])]
    return [*missing, *bad_schema, *bad_strings, *bad_budget, *bad_lists]


def decision_from_wire(wire: object) -> Decision | list[str]:
    if not isinstance(wire, dict):
        return [f"wire form must be a dict, got {type(wire).__name__}"]
    errors = _errors(wire)
    if errors:
        return errors
    return Decision(
        model=wire["model"],
        effort=wire["effort"],
        budget_usd=float(wire["budget_usd"]),
        reasons=tuple(wire["reasons"]),
        clipped_by=tuple(wire["clipped_by"]),
        chosen_class=wire["chosen_class"],
    )
