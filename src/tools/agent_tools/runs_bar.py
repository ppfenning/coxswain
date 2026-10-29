"""Pure core for `cox runs bar`: a one-line Waybar custom-module JSON body
built from the same per-run rows `runs_top.render` draws.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from agent_tools import chair

__all__ = ["attention", "bar"]

_ATTENTION_STATUSES = ("quarantined", "budget")
_NO_RUNS = "all lanes clear"


def _lost(r: Mapping, heartbeat_minutes: int) -> bool:
    """A remote lane whose heartbeat is older than the chair's window: a dead host or a dropped lane."""
    age = r.get("heartbeat_age")
    return bool(r.get("remote")) and age is not None and age > heartbeat_minutes * 60


def attention(rows: Sequence[Mapping], heartbeat_minutes: int = chair.DEFAULT_HEARTBEAT_MINUTES) -> bool:
    """A quarantined or budget-stopped row, or a remote lane silent past `heartbeat_minutes`."""
    return any(r["status"] in _ATTENTION_STATUSES or _lost(r, heartbeat_minutes) for r in rows)


def bar(rows: Sequence[Mapping], attention: bool) -> dict:
    live = [r for r in rows if r["alive"]]
    n = len(live)
    cost = sum(r["cost_usd"] for r in live)
    tooltip = "\n".join(f"{r['phase']} · {r['node']}" for r in live) if live else _NO_RUNS
    cls = "attention" if attention else ("running" if n else "idle")
    return {"text": f"{n} runs · ${cost:.2f}", "tooltip": tooltip, "class": cls}
