"""Pure check of a ticket's no-caller, only-user and no-import claims against search hits. No I/O: `search` is injected."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

Hit = tuple[str, int, str]
Kind = Literal["no_caller", "only_user", "no_import"]

_MAX_LISTED = 5
_SURFACE_MARKER = " (a surface of this task)"

_SUBJECT = r"`?(?P<s>[A-Za-z_]\w*(?:\.\w+)*)`?"
_FILE = r"`?(?P<f>[\w/-]+(?:\.[\w-]+)*)`?"
_PATTERNS: tuple[tuple[Kind, re.Pattern[str]], ...] = tuple(
    (kind, re.compile(rf"(?<!\w){text}", re.IGNORECASE))
    for kind, text in (
        ("no_caller", rf"no\s+callers?\s+(?:of|uses?)\s+{_SUBJECT}"),
        ("no_caller", rf"{_SUBJECT}\s+has\s+no\s+callers?\b"),
        ("no_caller", rf"nothing\s+calls\s+{_SUBJECT}"),
        ("only_user", rf"only\s+{_FILE}\s+uses\s+{_SUBJECT}"),
        ("only_user", rf"{_SUBJECT}\s+is\s+used\s+only\s+by\s+{_FILE}"),
        ("no_import", rf"(?:nothing|no\s+module)\s+imports\s+{_SUBJECT}"),
        ("no_import", rf"{_SUBJECT}\s+is\s+imported\s+by\s+nothing\b"),
    )
)
_IMPORT_LINE = re.compile(r"^\s*(?:import|from)\b")


@dataclass(frozen=True)
class Claim:
    kind: Kind
    subject: str
    allowed: tuple[str, ...] = ()


def parse_claims(body: str) -> tuple[Claim, ...]:
    """Claims in `body`, in the order they appear."""
    found = (
        (m.start(), Claim(kind, m.group("s"), (m.group("f"),) if "f" in m.re.groupindex else ()))
        for kind, pattern in _PATTERNS
        for m in pattern.finditer(body)
    )
    return tuple(claim for _, claim in sorted(found, key=lambda pair: pair[0]))


def _covers(path: str, entry: str) -> bool:
    """Whole-segment suffix match: `entry` is `path`, or `path` ends in `/entry`."""
    return path == entry or path.endswith(f"/{entry}")


def _is_definition(subject: str, text: str) -> bool:
    return re.match(rf"\s*(?:async\s+)?(?:def|class)\s+{re.escape(subject.rsplit('.', 1)[-1])}\b", text) is not None


def _quoted(claim: Claim) -> str:
    return {
        "no_caller": f"no caller of {claim.subject}",
        "only_user": f"only {', '.join(claim.allowed)} uses {claim.subject}",
        "no_import": f"nothing imports {claim.subject}",
    }[claim.kind]


def _offending(claim: Claim, hits: Sequence[Hit]) -> tuple[Hit, ...]:
    """Hits that make the claim false."""
    counted = tuple(h for h in hits if claim.kind != "no_import" or _IMPORT_LINE.match(h[2]))
    definitions = tuple(h for h in counted if _is_definition(claim.subject, h[2]))
    others = tuple(h for h in counted if h not in definitions)
    if claim.kind != "only_user":
        return others
    home = {path for path, _, _ in definitions}
    return tuple(h for h in others if h[0] not in home and not any(_covers(h[0], a) for a in claim.allowed))


def _correction(claim: Claim, hits: Sequence[Hit], surfaces: Sequence[str]) -> str:
    listed = tuple(
        f"{path}:{line}: {text.strip()}" + (_SURFACE_MARKER if any(_covers(path, s) for s in surfaces) else "")
        for path, line, text in hits[:_MAX_LISTED]
    )
    more = (f"and {len(hits) - _MAX_LISTED} more",) if len(hits) > _MAX_LISTED else ()
    noun = "hit" if len(hits) == 1 else "hits"
    return f"claim '{_quoted(claim)}' is false, {len(hits)} {noun}: " + "; ".join((*listed, *more))


def check_claims(task: Mapping[str, object], search: Callable[[str], Sequence[Hit]]) -> tuple[str, ...]:
    """One correction per false claim in the task body, in body order. A true claim gives nothing."""
    body = task.get("body")
    raw = task.get("surfaces")
    surfaces = tuple(str(s) for s in raw) if isinstance(raw, (list, tuple)) else ()
    pairs = tuple((c, _offending(c, search(c.subject))) for c in parse_claims(body if isinstance(body, str) else ""))
    return tuple(_correction(claim, hits, surfaces) for claim, hits in pairs if hits)
