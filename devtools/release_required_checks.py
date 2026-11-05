"""Judge required status checks against `gh pr checks --json name,bucket` output. Pure: no subprocess, clock or environment."""

import json
from dataclasses import dataclass
from typing import Any, Literal

Kind = Literal["green", "pending", "failed", "missing"]

_GREEN = frozenset({"pass", "skipping"})
_FAILED = frozenset({"fail", "cancel"})


@dataclass(frozen=True)
class Judgement:
    kind: Kind
    names: tuple[str, ...] = ()


def _load(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def required_names(protection_json: str) -> list[str] | None:
    """Names from `checks[].context` and legacy `contexts[]`, first-seen order; None when unreadable or neither key is present."""
    doc = _load(protection_json)
    if not isinstance(doc, dict) or not ("checks" in doc or "contexts" in doc):
        return None
    checks = doc.get("checks")
    contexts = doc.get("contexts")
    found = [c.get("context") for c in checks if isinstance(c, dict)] if isinstance(checks, list) else []
    found += contexts if isinstance(contexts, list) else []
    return list(dict.fromkeys(n for n in found if isinstance(n, str)))


def _buckets(reported_json: str) -> dict[str, set[str]]:
    rows = _load(reported_json)
    entries = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    return {
        name: {str(r.get("bucket")) for r in entries if r.get("name") == name}
        for name in {r.get("name") for r in entries if isinstance(r.get("name"), str)}
    }


def _state(buckets: set[str] | None) -> Kind:
    if buckets is None:
        return "missing"
    if buckets & _FAILED:
        return "failed"
    # A bucket outside the five known values is not green, so it keeps the wait open as pending.
    if buckets - _GREEN:
        return "pending"
    return "green"


def judge(required: list[str], reported_json: str) -> Judgement:
    """Precedence failed, missing, pending, green. Names are those of the winning kind, in required order."""
    reported = _buckets(reported_json)
    states = [(name, _state(reported.get(name))) for name in required]
    for kind in ("failed", "missing", "pending"):
        names = tuple(n for n, s in states if s == kind)
        if names:
            return Judgement(kind, names)
    return Judgement("green", tuple(required))


def render_missing(names: list[str] | tuple[str, ...], timeout_s: float) -> str:
    return f"required checks not reported within {int(timeout_s)}s: {', '.join(names)}"
