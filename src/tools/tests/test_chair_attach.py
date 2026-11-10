import json
from pathlib import Path

from agent_tools import chair_attach
from agent_tools.chair_resolve import SessionHolder


def _holder(kind, label="chair-a"):
    return SessionHolder(kind=kind, label=label)


def test_loop_holder_with_another_label_gives_mcp():
    assert chair_attach.attach_mode(_holder("loop"), "me", False) == "mcp"


def test_force_workspace_gives_workspace():
    assert chair_attach.attach_mode(_holder("loop"), "me", True) == "workspace"


def test_free_holder_gives_workspace():
    assert chair_attach.attach_mode(_holder("free", None), "me", False) == "workspace"


def test_stale_holder_gives_workspace():
    assert chair_attach.attach_mode(_holder("stale"), "me", False) == "workspace"


def test_session_holder_gives_workspace():
    assert chair_attach.attach_mode(_holder("session"), "me", False) == "workspace"


def test_no_holder_gives_workspace():
    assert chair_attach.attach_mode(None, "me", False) == "workspace"


def test_loop_holder_with_own_label_gives_workspace():
    assert chair_attach.attach_mode(_holder("loop", "me"), "me", False) == "workspace"


def test_mcp_config_carries_id_and_single_arg():
    assert chair_attach.mcp_config("cox", "abc") == {
        "mcpServers": {
            "coxswain-chair": {
                "type": "stdio",
                "command": "cox",
                "args": ["mcp"],
                "env": {"COXSWAIN_MCP_CONVERSATION": "abc"},
            }
        }
    }


def test_attach_argv_with_plugin_root():
    config = {"a": 1}
    assert chair_attach.attach_argv("/plug", config, []) == [
        "claude",
        "--mcp-config",
        '{"a": 1}',
        "--plugin-dir",
        "/plug",
    ]


def test_attach_argv_without_plugin_root():
    assert chair_attach.attach_argv(None, {"a": 1}, []) == ["claude", "--mcp-config", '{"a": 1}']


def test_attach_argv_keeps_extra_args_last_and_in_order():
    argv = chair_attach.attach_argv("/plug", {}, ["--model", "haiku"])
    assert argv[-2:] == ["--model", "haiku"]
    assert "--resume" not in argv
    assert json.loads(argv[2]) == {}


def test_read_conversation_valid_text():
    assert chair_attach.read_conversation('{"id": "abc"}') == "abc"


def test_read_conversation_empty_text():
    assert chair_attach.read_conversation("") is None


def test_read_conversation_none():
    assert chair_attach.read_conversation(None) is None


def test_read_conversation_bad_json():
    assert chair_attach.read_conversation("{nope") is None


def test_read_conversation_missing_or_non_string_id():
    assert chair_attach.read_conversation("{}") is None
    assert chair_attach.read_conversation('{"id": 7}') is None
    assert chair_attach.read_conversation("[1]") is None


def test_conversation_path_with_state_home():
    assert chair_attach.conversation_path("/s", "/h") == Path("/s/coxswain/mcp-conversation.json")


def test_conversation_path_without_state_home():
    expected = Path("/h/.local/state/coxswain/mcp-conversation.json")
    assert chair_attach.conversation_path("", "/h") == expected
    assert chair_attach.conversation_path(None, "/h") == expected


def test_conversation_text_round_trips():
    assert chair_attach.read_conversation(chair_attach.conversation_text("abc")) == "abc"
