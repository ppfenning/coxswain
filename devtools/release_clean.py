"""The pure half of the release's clean gate: which tree of which checkout a
release pins, and the refusal it owes for each personal reference the
checker (`.github/check_clean.py`) found there. No filesystem, subprocess or
environment here — `cli.py` exports each tree, runs the checker over it and
hands the hits back.

A component is checked at the exact ref the release will pin: a `pinned`
component at the manifest tag it keeps, a component this release tags at its
default branch's tip (the sync gate holds HEAD level with it before any tag
is made), and the umbrella likewise when the plan tags it."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

UMBRELLA = "coxswain"

# The checker's line: `<path>:<line>: <rule>: '<found>'`. The found text is
# dropped on purpose: under the names rule it is the very name the gate keeps
# out of a release, and a refusal line can travel further (a push notice).
_HIT_RE = re.compile(r"^(?P<path>.+?):(?P<line>\d+): (?P<rule>[\w-]+): ")


def clean_targets(steps: Sequence[dict]) -> list[tuple[str, str | None]]:
    """`(component, ref)` per tree the plan pins, in plan order: a `pinned`
    step's own tag, None (the default branch's tip) for a `tag` or `rejoin`
    step, and the umbrella at None when a `tag_self` step tags it."""
    pinned = [(s["component"], s["tag"]) for s in steps if s["kind"] == "pinned"]
    tagged = [(s["component"], None) for s in steps if s["kind"] in ("tag", "rejoin")]
    umbrella = [(UMBRELLA, None)] if any(s["kind"] == "tag_self" for s in steps) else []
    return list(dict.fromkeys(pinned + tagged + umbrella))


def hit_location(hit: str) -> str:
    """`<path>:<line> <rule>` from one checker line, the matched text left out;
    a line that does not parse is kept whole rather than lost."""
    m = _HIT_RE.match(hit)
    return f"{m['path']}:{m['line']} {m['rule']}" if m else hit


def refusals(results: Mapping[str, tuple[str, list[str] | None]]) -> list[dict]:
    """One `refuse` step per component whose tree at `ref` holds a hit, naming
    every `file:line rule`; one per component whose tree could not be
    exported (None), since an unchecked tree is not a clean one. A component
    with no hits owes nothing."""
    def refusal(component: str, ref: str, hits: list[str] | None) -> dict | None:
        if hits is None:
            detail = f"could not export {ref} to check it for personal references; refusing rather than ship it unchecked"
        elif hits:
            detail = f"personal reference at {ref}: " + "; ".join(hit_location(h) for h in hits)
        else:
            return None
        return {"kind": "refuse", "component": component, "detail": detail}

    found = (refusal(component, ref, hits) for component, (ref, hits) in results.items())
    return [step for step in found if step is not None]


def unusable_deny() -> list[dict]:
    """The refusal when COXSWAIN_CLEAN_DENY is set but is no regex: the names
    rule cannot run, so no tree can be called clean. The pattern itself is
    never echoed."""
    return [{"kind": "refuse", "component": "clean",
             "detail": "COXSWAIN_CLEAN_DENY is not a usable regex; the names rule cannot run"}]
