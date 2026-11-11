"""Pure planner for the MCP-attached bare launch: mode, MCP config and argv. No I/O, clock or environment."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from agent_tools.chair_resolve import SessionHolder

SERVER_NAME = "coxswain-chair"
CONVERSATION_ENV = "COXSWAIN_MCP_CONVERSATION"


def attach_mode(holder: SessionHolder | None, own_label: str, force_workspace: bool) -> Literal["mcp", "workspace"]:
    """mcp only for a live chair loop under another label and no force_workspace."""
    live_other = holder is not None and holder.kind == "loop" and holder.label != own_label
    return "mcp" if live_other and not force_workspace else "workspace"


def mcp_config(entry: str, conversation_id: str) -> dict[str, Any]:
    """One stdio MCP server running `entry mcp`; the env carries the conversation id the server resumes."""
    return {
        "mcpServers": {
            SERVER_NAME: {
                "type": "stdio",
                "command": entry,
                "args": ["mcp"],
                "env": {CONVERSATION_ENV: conversation_id},
            }
        }
    }


def attach_argv(plugin_root: Path | str | None, config: Mapping[str, Any], extra_args: Sequence[str]) -> list[str]:
    """The claude argv: the config as one JSON string, the plugin dir when given, then extra_args last."""
    plugin = [] if plugin_root is None else ["--plugin-dir", str(plugin_root)]
    return ["claude", "--mcp-config", json.dumps(config), *plugin, *extra_args]


def conversation_path(state_home: str | None, home: Path) -> Path:
    """The stored conversation id file, under state_home when it is a non-empty string, else under home."""
    base = Path(state_home) if state_home else home / ".local" / "state"
    return base / "coxswain" / "mcp-conversation.json"


def read_conversation(text: str | None) -> str | None:
    """The stored id, or None for None, empty or invalid text, or a missing or non-string id."""
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    conversation_id = data.get("id") if isinstance(data, dict) else None
    return conversation_id if isinstance(conversation_id, str) else None


def conversation_text(conversation_id: str) -> str:
    """The JSON text to store for a conversation id."""
    return json.dumps({"id": conversation_id})
