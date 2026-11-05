"""Shadow-trial report for the cheap tier, per role.

Pure core over already-parsed `runs/<run>.usage.json` dicts: the CLI edge reads
the store and prints. Nothing here reads a clock, a profile or writes a file.
"""

from __future__ import annotations

import datetime
import itertools
from dataclasses import dataclass
from typing import Any

from agent_tools.stats_system_one import MAX_COSTLY_RATE, MIN_AGREEMENT, MIN_ROWS

NOTE = "This report changes nothing. Only an operator moves a role."


@dataclass(frozen=True)
class Row:
    role: str
    ts: datetime.datetime
    shadow_model_id: str | None
    shadow_tier: str | None
    shadow_verdict: bool | None
    shadow_agreed: bool
    shadow_costly: bool | None
    acting_cost: float
    shadow_cost: float


@dataclass(frozen=True)
class Summary:
    role: str
    shadow_model_id: str | None
    shadow_tier: str | None
    first: datetime.datetime
    last: datetime.datetime
    rows: int
    agreed: int
    costly: int
    acting_cost: float
    shadow_cost: float

    @property
    def agreement(self) -> float:
        return self.agreed / self.rows

    @property
    def costly_rate(self) -> float:
        return self.costly / self.rows

    @property
    def saved(self) -> float:
        """Acting cost minus shadow cost, summed over the window."""
        return self.acting_cost - self.shadow_cost

    @property
    def saved_per_call(self) -> float:
        return self.saved / self.rows


def _num(x: Any) -> float:
    """A missing, null or non-numeric cost counts as zero."""
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else 0.0


def _row(call: dict[str, Any]) -> Row | None:
    decision = call.get("decision")
    if not isinstance(decision, dict) or decision.get("shadow_agreed") is None:
        return None
    try:
        ts = datetime.datetime.fromisoformat(call["ts"])
    except (KeyError, TypeError, ValueError):
        return None
    return Row(
        role=decision.get("role") or call.get("role") or "",
        ts=ts,
        shadow_model_id=decision.get("shadow_model_id"),
        shadow_tier=decision.get("shadow_tier"),
        shadow_verdict=decision.get("shadow_verdict"),
        shadow_agreed=bool(decision["shadow_agreed"]),
        shadow_costly=decision.get("shadow_costly"),
        acting_cost=_num(call.get("cost_usd")),
        shadow_cost=_num(decision.get("shadow_cost_usd")),
    )


def rows_from_usage(usages: list[dict[str, Any]]) -> list[Row]:
    """Calls whose decision carries a non-null `shadow_agreed`. Every other call, the cheap-tier calls included, is skipped."""
    calls = (c for u in usages for c in (u.get("calls") or []) if isinstance(c, dict))
    return [r for c in calls if (r := _row(c)) is not None and r.role]


def window(rows: list[Row], role: str, since: str | None) -> list[Row]:
    """The role's rows on or after `since`, cut to the trailing run that shares one shadow_model_id."""
    mine = sorted(
        (r for r in rows if r.role == role and (since is None or r.ts.date().isoformat() >= since)),
        key=lambda r: r.ts,
    )
    return list(reversed(list(itertools.takewhile(lambda r: r.shadow_model_id == mine[-1].shadow_model_id, reversed(mine))))) if mine else []


def summarize(role: str, rows: list[Row]) -> Summary | None:
    """None when the window holds no rows. Costly is the flag the decision recorded, not recomputed here."""
    return (
        Summary(
            role=role,
            shadow_model_id=rows[-1].shadow_model_id,
            shadow_tier=rows[-1].shadow_tier,
            first=rows[0].ts,
            last=rows[-1].ts,
            rows=len(rows),
            agreed=sum(1 for r in rows if r.shadow_agreed),
            costly=sum(1 for r in rows if r.shadow_costly is True),
            acting_cost=sum(r.acting_cost for r in rows),
            shadow_cost=sum(r.shadow_cost for r in rows),
        )
        if rows
        else None
    )


def summaries(rows: list[Row], since: str | None, role: str | None) -> list[Summary]:
    roles = sorted({r.role for r in rows} if role is None else {role})
    return [s for ro in roles if (s := summarize(ro, window(rows, ro, since))) is not None]


def verdict(s: Summary) -> tuple[str, list[str]]:
    """("READY", []) or ("NOT YET", the unmet bars, each with its number)."""
    unmet = [
        *([f"rows {s.rows} < {MIN_ROWS}"] if s.rows < MIN_ROWS else []),
        *([f"agreement {s.agreement:.3f} < {MIN_AGREEMENT}"] if s.agreement < MIN_AGREEMENT else []),
        *([f"costly rate {s.costly_rate:.3f} > {MAX_COSTLY_RATE}"] if s.costly_rate > MAX_COSTLY_RATE else []),
    ]
    return ("NOT YET", unmet) if unmet else ("READY", [])


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def render_role(s: Summary) -> str:
    label, unmet = verdict(s)
    return "\n".join([
        f"{s.role}: {label}" + (f" ({'; '.join(unmet)})" if unmet else ""),
        f"  window: {s.first.date().isoformat()} to {s.last.date().isoformat()}, shadow model {s.shadow_model_id}, tier {s.shadow_tier}",
        f"  rows {s.rows}",
        f"  agreement {_pct(s.agreement)} ({s.agreed} of {s.rows})",
        f"  costly disagreements {s.costly} ({_pct(s.costly_rate)} of rows)",
        f"  acting cost ${s.acting_cost:.4f}, shadow cost ${s.shadow_cost:.4f}",
        f"  saved ${s.saved:.4f}, ${s.saved_per_call:.4f} per call",
    ])


def render_report(ss: list[Summary]) -> str:
    body = "\n\n".join(render_role(s) for s in ss) if ss else "no shadow rows in any usage file"
    return f"{body}\n\n{NOTE}"


def to_json(ss: list[Summary]) -> dict[str, Any]:
    def one(s: Summary) -> dict[str, Any]:
        label, unmet = verdict(s)
        return {
            "role": s.role, "shadow_model_id": s.shadow_model_id, "shadow_tier": s.shadow_tier,
            "first": s.first.isoformat(), "last": s.last.isoformat(),
            "rows": s.rows, "agreed": s.agreed, "agreement": s.agreement,
            "costly": s.costly, "costly_rate": s.costly_rate,
            "acting_cost": s.acting_cost, "shadow_cost": s.shadow_cost,
            "saved": s.saved, "saved_per_call": s.saved_per_call,
            "verdict": label, "unmet": unmet,
        }

    return {"roles": [one(s) for s in ss]}
