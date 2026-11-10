"""Pure planner for the MCP-attached chair session: mode choice, MCP config, argv, stored conversation id."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from agent_tools.chair_resolve import SessionHolder

SERVER = "coxswain-chair"
CONVERSATION_ENV = "COXSWAIN_MCP_CONVERSATION"
_FILE = "mcp-conversation.json"


def attach_mode(holder: SessionHolder | None, own_label: str, force_workspace: bool) -> Literal["mcp", "workspace"]:
    """mcp only for a live chair loop held by another label and no forced workspace."""
    attach = not force_workspace and holder is not None and holder.kind == "loop" and holder.label != own_label
    return "mcp" if attach else "workspace"


def mcp_config(entry: str, conversation_id: str) -> dict[str, Any]:
    """One stdio MCP server running `entry mcp`, told which conversation to resume."""
    server = {"type": "stdio", "command": entry, "args": ["mcp"], "env": {CONVERSATION_ENV: conversation_id}}
    return {"mcpServers": {SERVER: server}}


def attach_argv(plugin_root: str | None, config: Mapping[str, Any], extra_args: Sequence[str]) -> list[str]:
    """Claude Code argv with the config as one JSON string; no resume flag, no working directory."""
    plugin = [] if plugin_root is None else ["--plugin-dir", plugin_root]
    return ["claude", "--mcp-config", json.dumps(config, sort_keys=True), *plugin, *extra_args]


def conversation_path(state_home: str | None, home: str) -> Path:
    """An empty or missing state home falls back to `.local/state` under home."""
    base = Path(state_home) if state_home else Path(home) / ".local" / "state"
    return base / "coxswain" / _FILE


def read_conversation(text: str | None) -> str | None:
    """The stored id, or None for missing, empty, malformed or non-string content."""
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    found = data.get("id") if isinstance(data, dict) else None
    return found if isinstance(found, str) else None


def conversation_text(conversation_id: str) -> str:
    """The JSON text to store for `conversation_id`."""
    return json.dumps({"id": conversation_id})
