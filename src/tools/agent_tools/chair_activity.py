"""Pure assembly of the dash feed's `chair_agents` and `spend_split` sections."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from agent_tools.chair_activity_read import AgentTranscript, SessionTranscript
from agent_tools.chair_agents import agent_rows
from agent_tools.chair_spend_nodes import cost_per_done_task, group_spend_nodes
from agent_tools.chair_spend_split import spend_split
from agent_tools.operator_sessions import session_rows, window_costs
from agent_tools.stats_chair import chair_cost, lines_since

_WINDOWS = ("five_hour", "weekly")
_Cell = float | str | None


def _cell(value: object) -> _Cell:
    """A window's meter or seat spend as `spend_split` reads it: a number, a string or unset."""
    return value if value is None or isinstance(value, float | int | str) else None


def _window_inputs(
    window: Mapping[str, object],
    session_lines: Sequence[str],
    agents: Sequence[AgentTranscript],
    prices: Mapping[str, Mapping[str, float]],
    sessions: Sequence[SessionTranscript] | None = None,
) -> dict[str, _Cell]:
    """`lines_since` compares dates, so the ISO `start` is cut to its YYYY-MM-DD day."""
    day = str(window["start"])[:10]
    if sessions is None:
        sessions_usd = float(chair_cost(lines_since(session_lines, day), prices))
        agents_usd = float(sum(chair_cost(lines_since(a["lines"], day), prices) for a in agents))
    else:
        sessions_usd, agents_usd = window_costs(sessions, prices, day)
    return {
        "chair_session_usd": sessions_usd,
        "chair_agents_usd": agents_usd,
        "cox_seats_usd": _cell(window["seat_usd"]),
        "meter_fraction": _cell(window["meter_fraction"]),
        "resets_at": _cell(window["resets_at"]),
    }


def _node_fields(window_out: Mapping[str, float | str], rows: Sequence[Mapping[str, object]], done: int) -> dict[str, object]:
    """`spend_split` has a fixed key set, so the node fields are added to its window after it returns. `done` is the
    window's completions: tasks done, or approved and merged into their phase or PR branch, or landed on main, at the
    earliest such stamp in the window (`dash_feed._window_node_inputs`). `cost_per_completion` is the cox-seat spend
    over them. `cost_per_landed_task` is its deprecated alias with the same value, kept one release so an older towpath
    still reads a value. Despite its name, it is not a cost per task landed on main."""
    per_completion = round(cost_per_done_task(float(window_out["cox_seats_usd"]), done), 4)  # 96.9 / 6 is 16.150000000000002
    return {
        **window_out,
        "by_node": group_spend_nodes(rows),
        "cost_per_landed_task": per_completion,
        "completions": done,
        "cost_per_completion": per_completion,
    }


def gather_activity(
    session_lines: Sequence[str],
    agents: Sequence[AgentTranscript],
    prices: Mapping[str, Mapping[str, float]],
    now: datetime,
    windows: Mapping[str, Mapping[str, object]],
    node_rows: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
    done_counts: Mapping[str, int] | None = None,
    *,
    sessions: Sequence[SessionTranscript] | None = None,
) -> dict[str, object]:
    """`node_rows` and `done_counts` are keyed by window name; the caller has already cut them to that window.

    `sessions` are every gathered operator session; without them the one session is `session_lines` and `agents`,
    and the result has no `operator_sessions` key.
    """
    split = spend_split(
        {name: _window_inputs(windows[name], session_lines, agents, prices, sessions) for name in _WINDOWS}
    )
    return {
        "chair_agents": agent_rows(agents, prices, now),
        "spend_split": {
            name: _node_fields(split[name], (node_rows or {}).get(name, ()), (done_counts or {}).get(name, 0))
            for name in _WINDOWS
        },
        **({"operator_sessions": session_rows(sessions, prices, windows)} if sessions is not None else {}),
    }
