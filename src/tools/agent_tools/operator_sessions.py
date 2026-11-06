"""Pure operator-session rows, window costs and sources for the dash feed."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from agent_tools.stats_chair import chair_cost, lines_since

_WINDOWS = ("five_hour", "weekly")
_DESCRIPTION_CAP = 120

Prices = Mapping[str, Mapping[str, float]]


def _entry(line: str) -> dict | None:
    try:
        parsed = json.loads(line)
    except TypeError, ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _utc_second(raw: object) -> str | None:
    """`raw` as YYYY-MM-DDTHH:MM:SSZ in UTC; a naive time is read as UTC. None when it does not parse."""
    try:
        moment = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    utc = moment.astimezone(UTC) if moment.tzinfo else moment
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def _first_timestamp(lines: Sequence[str]) -> str | None:
    stamps = (_utc_second((entry or {}).get("timestamp")) for entry in map(_entry, lines))
    return next((s for s in stamps if s is not None), None)


def _first_model(lines: Sequence[str]) -> str:
    messages = (
        entry.get("message") for entry in map(_entry, lines) if entry is not None and entry.get("type") == "assistant"
    )
    models = (m.get("model") for m in messages if isinstance(m, dict) and isinstance(m.get("model"), str))
    return next(models, "")


def _agent_type(meta: Mapping) -> str:
    return next((str(meta[k]) for k in ("agentType", "subagent_type") if meta.get(k)), "")


def _description(meta: Mapping) -> str:
    lines = str(meta.get("description") or "").splitlines()
    return (lines[0] if lines else "")[:_DESCRIPTION_CAP]


def _agent_row(agent: Mapping, day: str, prices: Prices) -> dict | None:
    """None when the agent has no lines since `day`."""
    windowed = lines_since(agent["lines"], day)
    meta = agent.get("meta") or {}
    return (
        {
            "agent_type": _agent_type(meta),
            "description": _description(meta),
            "cost_usd": round(chair_cost(windowed, prices), 4),
        }
        if windowed
        else None
    )


def _session_row(session: Mapping, day: str, prices: Prices) -> dict | None:
    """None when the session has no lines since `day` or no parsable timestamp."""
    windowed = lines_since(session["lines"], day)
    started = _first_timestamp(session["lines"])
    if not windowed or started is None:
        return None
    agents = sorted(
        (row for row in (_agent_row(a, day, prices) for a in session["agents"]) if row is not None),
        key=lambda row: row["cost_usd"],
        reverse=True,
    )
    return {
        "session_id": session["session_id"],
        "started_at": started,
        "model": _first_model(session["lines"]),
        "cost_usd": round(chair_cost(windowed, prices), 4),
        "agent_count": len(agents),
        "agents": agents,
    }


def session_rows(sessions: Sequence[Mapping], prices: Prices, windows: Mapping[str, Mapping]) -> dict[str, list[dict]]:
    """Rows per window, newest session first; `lines_since` compares dates, so the ISO `start` is cut to its day."""
    return {
        name: sorted(
            (
                row
                for row in (_session_row(s, str(windows[name]["start"])[:10], prices) for s in sessions)
                if row is not None
            ),
            key=lambda row: row["started_at"],
            reverse=True,
        )
        for name in _WINDOWS
    }


def window_costs(sessions: Sequence[Mapping], prices: Prices, day: str) -> tuple[float, float]:
    """(main transcripts, agent transcripts) dollars since `day`, unrounded."""
    sessions_usd = sum(chair_cost(lines_since(s["lines"], day), prices) for s in sessions)
    agents_usd = sum(chair_cost(lines_since(a["lines"], day), prices) for s in sessions for a in s["agents"])
    return float(sessions_usd), float(agents_usd)


def sources(sessions: Sequence[Mapping], prices: Prices) -> dict:
    """`sessions_priced` is "ok" when any main transcript prices above zero, else "none"."""
    priced = any(chair_cost(s["lines"], prices) > 0 for s in sessions)
    return {"sessions_found": len(sessions), "sessions_priced": "ok" if priced else "none"}


def with_holder(
    sessions: Sequence[Mapping], holder: str, holder_lines: Sequence[str], holder_agents: Sequence[Mapping]
) -> list[dict]:
    """The lease holder's session is added only when no gathered `session_id` starts with `holder`."""
    gathered = [dict(s) for s in sessions]
    if holder and not any(str(s["session_id"]).startswith(holder) for s in sessions):
        return [*gathered, {"session_id": holder, "lines": list(holder_lines), "agents": list(holder_agents)}]
    return gathered
