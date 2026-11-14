import json
from pathlib import Path

from agent_tools.chair_attach import (
    attach_argv,
    attach_mode,
    conversation_path,
    conversation_text,
    mcp_config,
    read_conversation,
)
from agent_tools.chair_resolve import SessionHolder


def test_loop_holder_with_another_label_gives_mcp():
    assert attach_mode(SessionHolder("loop", "chair-a"), "me", False) == "mcp"


def test_force_workspace_gives_workspace():
    assert attach_mode(SessionHolder("loop", "chair-a"), "me", True) == "workspace"


def test_free_holder_gives_workspace():
    assert attach_mode(SessionHolder("free"), "me", False) == "workspace"


def test_stale_holder_gives_workspace():
    assert attach_mode(SessionHolder("stale", "chair-a"), "me", False) == "workspace"


def test_session_holder_gives_workspace():
    assert attach_mode(SessionHolder("session", "chair-a"), "me", False) == "workspace"


def test_no_holder_gives_workspace():
    assert attach_mode(None, "me", False) == "workspace"


def test_loop_holder_with_own_label_gives_workspace():
    assert attach_mode(SessionHolder("loop", "me"), "me", False) == "workspace"


def test_mcp_config_carries_id_and_single_arg():
    server = mcp_config("cox", "conv-1")["mcpServers"]["coxswain-chair"]
    assert server == {
        "type": "stdio",
        "command": "cox",
        "args": ["mcp"],
        "env": {"COXSWAIN_MCP_CONVERSATION": "conv-1"},
    }


def test_attach_argv_with_plugin_root():
    config = mcp_config("cox", "c")
    assert attach_argv(Path("/p"), config, []) == ["claude", "--mcp-config", json.dumps(config), "--plugin-dir", "/p"]


def test_attach_argv_without_plugin_root():
    config = mcp_config("cox", "c")
    assert attach_argv(None, config, []) == ["claude", "--mcp-config", json.dumps(config)]


def test_attach_argv_keeps_extra_args_last_and_in_order():
    argv = attach_argv("/p", mcp_config("cox", "c"), ["--model", "opus", "hello"])
    assert argv[-3:] == ["--model", "opus", "hello"]
    assert argv[3:5] == ["--plugin-dir", "/p"]


def test_read_conversation_valid_text():
    assert read_conversation('{"id": "abc"}') == "abc"


def test_read_conversation_empty_text():
    assert read_conversation("") is None


def test_read_conversation_none():
    assert read_conversation(None) is None


def test_read_conversation_bad_json():
    assert read_conversation("{not json") is None


def test_read_conversation_missing_or_non_string_id():
    assert read_conversation("{}") is None
    assert read_conversation('{"id": 7}') is None
    assert read_conversation("[1]") is None


def test_conversation_path_with_state_home():
    assert conversation_path("/s", Path("/h")) == Path("/s/coxswain/mcp-conversation.json")


def test_conversation_path_without_state_home():
    expected = Path("/h/.local/state/coxswain/mcp-conversation.json")
    assert conversation_path(None, Path("/h")) == expected
    assert conversation_path("", Path("/h")) == expected


def test_conversation_text_round_trips():
    assert read_conversation(conversation_text("conv-9")) == "conv-9"
