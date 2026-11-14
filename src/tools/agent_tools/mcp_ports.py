"""The MCP server's ports for a server that runs beside the chair: where each tool's facts come from and where its
writes go. Pure translation at the top; the edge that reads the workspace and the bus is at the bottom.

- reads: the dash feed (the board), the release facts, the chair's last why-not-launched reasons, the courier bus;
- plain writes: `mcp_actions.local_actor`, the `cox` command a person would type;
- requests to the chair and the record of what was done: one courier entry each, on the chair's chat topic.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_tools.mcp_protocol import ToolError

UNLAUNCHED_FILE = "chair.unlaunched.json"  # under runs/: the chair's last tick writes its why-not-launched reasons here
_ENVELOPE = ("caller", "conversation", "tool", "at")


def directive_payload(entry: Mapping[str, object]) -> dict[str, str]:
    """Pure. The chair-chat directive (`chair_chat.DIRECTIVE_KINDS`) for one MCP request to the chair.
    Raises ToolError for a tool the chair has no directive for: those are carried out by the server or not at all."""
    tool, kind = str(entry.get("tool") or ""), str(entry.get("kind") or "")

    def text(key: str) -> str:
        return str(entry.get(key) or "").strip()

    if tool == "cut_release":
        return {"kind": "cut_release", "version": text("version")}
    if tool == "set_priority":
        return {"kind": "priority", "target_kind": "initiative", "target": text("initiative"), "rank": text("priority")}
    if tool == "send_directive" and kind in ("hold", "release"):
        return {"kind": kind, "target_kind": "initiative", "target": text("initiative")}
    if tool == "send_directive" and kind == "scope":
        return {"kind": "scope", "initiatives": text("initiative")}
    if tool == "send_directive" and kind == "enable":
        return {"kind": "feature", "name": text("feature"), "state": "on"}
    raise ToolError(f"the chair takes no request for {tool}: it needs a server that runs beside the store")


def action_fields(entry: Mapping[str, object]) -> dict[str, str]:
    """Pure. A done or refused write as the flat text fields a bus entry carries."""
    return {key: json.dumps(value) if isinstance(value, (dict, list)) else str(value) for key, value in entry.items() if value is not None}


def bus_entry(event: str, entry: Mapping[str, object], message_id: str) -> dict:
    """Pure. The courier entry for one MCP send: a request to the chair, or the record of a write already made."""
    from agent_tools import chair_chat, courier

    fields = directive_payload(entry) if event == "chat_directive" else action_fields(entry)
    named = str(entry.get("initiative") or "").strip()
    ref = courier.Reference("initiative", named) if named else courier.Reference("proposal", f"mcp-{entry.get('tool') or 'request'}")
    lead = "directive" if event == "chat_directive" else str(entry.get("status") or "done")
    note = f"{lead}: {fields.get('kind') or entry.get('tool') or ''}"
    return courier.send(ref, str(entry.get("caller") or "mcp"), "chair", note, message_id, event=event, fields=fields, topic=chair_chat.LABEL)


def bus_events(text: str, since: str | None, limit: int) -> list[dict]:
    """Pure. The newest `limit` typed bus entries at or after `since`, as the recent_events tool reads them.
    An entry's time is the `at` its fields carry; one with none sorts first and passes any `since`."""
    found = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or not entry.get("event"):
            continue
        fields = entry.get("fields") if isinstance(entry.get("fields"), dict) else {}
        found.append({"type": entry["event"], "ts": str(fields.get("at") or ""), "subject": str(entry.get("ref") or entry.get("topic") or "")})
    kept = [e for e in found if not since or not e["ts"] or e["ts"] >= since]
    return sorted(kept, key=lambda e: e["ts"])[-limit:]


def unlaunched_reasons(text: str | None, initiative: str) -> list[dict]:
    """Pure. The reasons the chair's last tick gave for not launching `initiative`; [] when it gave none."""
    try:
        held = json.loads(text) if text else {}
    except ValueError:
        return []
    reasons = held.get("reasons") if isinstance(held, dict) else None
    return [r for r in reasons or () if isinstance(r, dict) and r.get("initiative") == initiative]


def reasons_text(items: Sequence[Mapping[str, Any]], at: str) -> str:
    """Pure. What the chair writes after a tick: when, and each initiative it did not launch with why."""
    reasons = [{"initiative": str(i.get("initiative")), "reason": str(i.get("reason") or "")} for i in items]
    return json.dumps({"at": at, "reasons": reasons}, indent=1) + "\n"


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def build(profile: Mapping[str, Any], profile_path: Path, known_epics: Callable[[], Sequence[str]]):
    """Edge. The server's `Ports` for the workspace `profile` names. Everything is read at call time, never cached:
    a long-lived server answers from the board as it is now."""
    from agent_tools import courier, dash_detail_release, dash_feed, mcp_actions
    from agent_tools.mcp_read_tools import ReadPorts
    from agent_tools.mcp_server import Ports

    workspace = Path(str(profile["workspace_dir"])).expanduser()
    runs_dir, bus = workspace / "runs", workspace / "courier.jsonl"

    def now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    def send(event: str, entry: dict) -> str:
        message_id = uuid.uuid4().hex
        courier.append_file(bus, bus_entry(event, entry, message_id))
        return message_id

    read = ReadPorts(
        snapshot=lambda: dash_feed.gather_feed(runs_dir, workspace, now(), dict(profile)),
        release=lambda: dash_detail_release.build(now()),
        unlaunched=lambda ident: unlaunched_reasons(_read_text(runs_dir / UNLAUNCHED_FILE), ident),
        events=lambda since, limit: bus_events(courier.read_text(bus), since, limit),
        settings=mcp_actions.settings_reader(profile_path),
    )
    repo_map = {str(name): str(path) for name, path in (profile.get("repo_map") or {}).items()}
    run = mcp_actions.local_actor(profile_path)

    def act(tool: str, args: Mapping[str, object]) -> tuple[bool, str]:
        # A repo may be named as the forge knows it (owner/name); the command takes it as the profile maps it.
        named = str(args.get("repo") or "")
        return run(tool, {**args, "repo": repo_map[named]} if named in repo_map else args)

    return Ports(read, send, now, known_repos=sorted({*repo_map, *repo_map.values()}), known_epics=list(known_epics()), act=act)
