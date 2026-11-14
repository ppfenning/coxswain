"""Read-only MCP tools over the chair: release status, queue board, explain and recent events.

Every tool answers compactly by default (counts and ids) and gives detail only when asked. A result over
MAX_RESULT_CHARS is cut and says which argument fetches the rest. All fetching is behind `ReadPorts`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import NamedTuple

from agent_tools.mcp_protocol import ToolError, ToolSpec

MAX_RESULT_CHARS = 4000  # len(json.dumps(result)) of one tool result
DEFAULT_LIMIT = 20
MAX_LIMIT = 200
QUEUE_STATES = ("ready", "building", "approved", "quarantined")
_DONE = ("merged", "landed")
_OPEN = ("ready", "building", "approved", "quarantined", "blocked")
_CLIP = 200


class ReadPorts(NamedTuple):
    """The caller's fetches: the dash feed, the release facts, one initiative's unlaunched reasons, bus events.
    `events(since, limit)` must return the newest `limit` events at or after `since`; the tool sorts, it does not select."""

    snapshot: Callable[[], dict]
    release: Callable[[], dict]
    unlaunched: Callable[[str], Sequence[Mapping]]
    events: Callable[[str | None, int], Sequence[Mapping]]
    # `cox settings get --json`: {"sections": [{"name", "rows": [{scope, key, value, source_file, tracked, operator_only}]}]}.
    # None on a server that runs away from the machine holding the cartridge: the tool then says so.
    settings: Callable[[], Mapping] | None = None


def _clip(text: object) -> str:
    return str(text)[:_CLIP]


def _brief(value: object) -> object:
    """A value as is when its JSON is short, else its clipped JSON text, so a fact cannot fill the ceiling."""
    text = json.dumps(value)
    return value if len(text) <= _CLIP else {"clipped": text[:_CLIP]}


def _largest_fitting(fits: Callable[[int], bool], low: int, high: int) -> int:
    if low >= high:
        return low
    mid = (low + high + 1) // 2
    return _largest_fitting(fits, mid, high) if fits(mid) else _largest_fitting(fits, low, mid - 1)


def fit(result: dict, key: str, more: str) -> dict:
    """Cut the list at `key` until the JSON of the result is within MAX_RESULT_CHARS; say how to fetch the rest."""
    items = result[key]

    def cut(n: int) -> dict:
        return {**result, key: items[:n], "truncated": True, "omitted": len(items) - n, "more": more}

    if len(json.dumps(result)) <= MAX_RESULT_CHARS:
        return result
    return cut(_largest_fitting(lambda n: len(json.dumps(cut(n))) <= MAX_RESULT_CHARS, 0, len(items)))


def board_initiatives(snapshot: Mapping) -> list[dict]:
    epics = (snapshot.get("board") or {}).get("epics") or ()
    return [i for epic in epics for i in epic.get("initiatives") or ()]


def _tasks(snapshot: Mapping) -> list[tuple[str, Mapping]]:
    return [
        (i["id"], t)
        for i in board_initiatives(snapshot)
        for phase in i.get("phases") or ()
        for t in phase.get("tasks") or ()
    ]


def board_tasks(snapshot: Mapping) -> list[dict]:
    """Every board task as {initiative, task, state, cost_usd}."""
    return [
        {"initiative": slug, "task": t["id"], "state": t["status"], "cost_usd": t.get("cost_usd") or 0.0}
        for slug, t in _tasks(snapshot)
    ]


def _find_initiative(snapshot: Mapping, ident: str) -> dict | None:
    return next((i for i in board_initiatives(snapshot) if ident in (i["id"], i.get("short_id"))), None)


def _done_open(initiative: Mapping) -> dict:
    counts = initiative.get("counts") or {}
    return {
        "done": sum(counts.get(s, 0) for s in _DONE),
        "open": sum(counts.get(s, 0) for s in _OPEN),
    }


def release_status(arguments: Mapping, ports: ReadPorts) -> dict:
    facts = ports.release()
    version = arguments.get("version")
    if version is not None and version != facts.get("version"):
        raise ToolError(f"release {version} not found; the current release is {facts.get('version')}")
    snapshot = ports.snapshot()
    ident = arguments.get("id")
    if ident is not None:
        found = _find_initiative(snapshot, ident)
        if found is None:
            raise ToolError(f"unknown initiative: {ident}")
        counts = {s: n for s, n in (found.get("counts") or {}).items() if n}
        return {"initiative": found["id"], **_done_open(found), "counts": counts}
    rows = [{"initiative": i["id"], **_done_open(i)} for i in board_initiatives(snapshot)]
    result = {
        **{k: _brief(facts.get(k)) for k in ("version", "last_cut", "pinned", "tap")},
        "totals": {k: sum(r[k] for r in rows) for k in ("done", "open")},
        "initiatives": rows,
    }
    return fit(result, "initiatives", "pass id=<initiative> for one initiative's per-status counts")


def queue_board(arguments: Mapping, ports: ReadPorts) -> dict:
    state, ident = arguments.get("state"), arguments.get("id")
    states = (state,) if state else QUEUE_STATES
    rows = [
        r
        for r in board_tasks(ports.snapshot())
        if r["state"] in states and ident in (None, r["initiative"])
    ]
    totals = {
        s: {
            "count": sum(1 for r in rows if r["state"] == s),
            "cost_usd": round(sum(r["cost_usd"] for r in rows if r["state"] == s), 4),
        }
        for s in states
    }
    more = "pass detail=true for rows, narrowed with state and id"
    if arguments.get("detail"):
        return fit({"totals": totals, "rows": rows}, "rows", more)
    return fit({"totals": totals, "initiatives": sorted({r["initiative"] for r in rows})}, "initiatives", more)


def _quarantine_reason(snapshot: Mapping, task_id: str) -> str | None:
    """The queue phase reason `dash_feed._waved` writes, "task <id> is quarantined: <why>", read for this task."""
    head = f"task {task_id} is quarantined: "
    reasons = (p.get("reason") or "" for row in snapshot.get("queue") or () for p in row.get("phases") or ())
    return next((r[len(head):] for r in reasons if r.startswith(head)), None)


def _run_attempt(snapshot: Mapping, run_id: str) -> int | None:
    """The attempt number of the live run the board task points at; a finished task has no run, so none."""
    return next((r["attempt"] for r in snapshot.get("runs") or () if run_id and r.get("run") == run_id), None)


def explain(arguments: Mapping, ports: ReadPorts) -> dict:
    ident = arguments["id"]
    snapshot = ports.snapshot()
    initiative = _find_initiative(snapshot, ident)
    if initiative is not None:
        reasons = [_clip(r.get("reason", "")) for r in ports.unlaunched(initiative["id"])]
        return {"kind": "initiative", "id": initiative["id"], "unlaunched": bool(reasons), "reasons": reasons}
    task = next((t for _, t in _tasks(snapshot) if t["id"] == ident), None)
    if task is None:
        raise ToolError(f"unknown id: {ident}")
    reason = _quarantine_reason(snapshot, ident) if task["status"] == "quarantined" else None
    attempt = _run_attempt(snapshot, task.get("run") or "")
    return {
        "kind": "task",
        "id": ident,
        "state": task["status"],
        **({"quarantine_reason": _clip(reason)} if reason else {}),
        **({"attempt": attempt} if attempt is not None else {}),
    }


def _typed_event(event: Mapping) -> dict:
    subject = next((event[k] for k in ("subject", "task", "initiative", "run") if event.get(k)), "")
    return {
        "type": str(event.get("type") or event.get("kind") or "event"),
        "ts": str(event.get("ts") or event.get("at") or ""),
        "subject": _clip(subject),
    }


def recent_events(arguments: Mapping, ports: ReadPorts) -> dict:
    limit = max(1, min(int(arguments.get("limit", DEFAULT_LIMIT)), MAX_LIMIT))
    since = arguments.get("since")
    typed = sorted((_typed_event(e) for e in ports.events(since, limit)), key=lambda e: e["ts"], reverse=True)
    result = {"events": typed[:limit], "limit": limit}
    return fit(result, "events", "pass since=<ts of the oldest event shown> or a smaller limit for the rest")


_SETTING_FIELDS = ("scope", "key", "value", "source_file", "tracked", "operator_only")


def cartridge_settings(arguments: Mapping, ports: ReadPorts) -> dict:
    """The cartridge's and profile's settings as the owner's own files hold them. Compact by default: each
    section's name and how many settings it has. `section` gives that section's rows and `key` one setting's.
    The server returns the facts; putting them into words is the caller's job."""
    if ports.settings is None:
        raise ToolError("this server cannot read the cartridge: it does not run on the machine that holds it")
    found = [(str(s.get("name")), list(s.get("rows") or ())) for s in ports.settings().get("sections") or ()]
    section, key = arguments.get("section"), arguments.get("key")
    rows = [
        {"section": name, **{f: (_clip(r.get(f)) if f == "value" and isinstance(r.get(f), str) else r.get(f)) for f in _SETTING_FIELDS}}
        for name, held in found
        for r in held
        if section in (None, name) and key in (None, r.get("key"))
    ]
    if section is not None and section not in [name for name, _ in found]:
        raise ToolError(f"unknown section: {section}; one of {', '.join(name for name, _ in found)}")
    if key is not None and not rows:
        raise ToolError(f"unknown setting: {key}")
    if section is None and key is None:
        return {"sections": [{"name": name, "settings": len(held)} for name, held in found]}
    return fit({"settings": rows}, "settings", "pass key=<one setting> for a single row")


def _schema(properties: Mapping[str, object], required: Sequence[str] = ()) -> dict:
    return {"type": "object", "properties": dict(properties), "required": list(required), "additionalProperties": False}


def read_tools(ports: ReadPorts) -> list[ToolSpec]:
    """The five read-only tools; none takes a confirm argument and none writes."""
    return [
        ToolSpec(
            "release_status",
            "Release facts and per-initiative done and open task counts. Compact by default; id gives one "
            "initiative's per-status counts.",
            _schema({"version": {"type": "string"}, "id": {"type": "string"}}),
            False,
            lambda arguments, caller: release_status(arguments, ports),
        ),
        ToolSpec(
            "queue_board",
            "Task counts and cost in USD per state (ready, building, approved, quarantined). Compact by "
            "default; detail=true returns the rows.",
            _schema(
                {
                    "state": {"type": "string", "enum": list(QUEUE_STATES)},
                    "detail": {"type": "boolean"},
                    "id": {"type": "string"},
                }
            ),
            False,
            lambda arguments, caller: queue_board(arguments, ports),
        ),
        ToolSpec(
            "explain",
            "Why an initiative did not launch, or a task's state, quarantine reason and live run attempt when present.",
            _schema({"id": {"type": "string"}}, ("id",)),
            False,
            lambda arguments, caller: explain(arguments, ports),
        ),
        ToolSpec(
            "recent_events",
            f"Recent events, newest first. limit defaults to {DEFAULT_LIMIT}, capped at {MAX_LIMIT}.",
            _schema({"limit": {"type": "integer"}, "since": {"type": "string"}}),
            False,
            lambda arguments, caller: recent_events(arguments, ports),
        ),
        ToolSpec(
            "cartridge_settings",
            "The cartridge's and profile's settings, read from their owner's files. Compact by default (section "
            "names and counts); section gives that section's settings, key gives one.",
            _schema({"section": {"type": "string"}, "key": {"type": "string"}}),
            False,
            lambda arguments, caller: cartridge_settings(arguments, ports),
        ),
    ]
