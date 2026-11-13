import json

from agent_tools.mcp_protocol import (
    SERVER_VERSION,
    ToolError,
    ToolSpec,
    decode,
    encode,
    handle,
    validate_arguments,
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["fast", "slow"]},
        "count": {"type": "integer"},
    },
    "required": ["mode"],
    "additionalProperties": False,
}


def _echo(arguments, caller):
    return {"mode": arguments["mode"], "caller": caller}


def _fail(arguments, caller):
    raise ToolError("lane is frozen")


def _crash(arguments, caller):
    raise RuntimeError("secret-detail")


READ = ToolSpec("read_it", "reads", _SCHEMA, False, _echo)
WRITE = ToolSpec("write_it", "writes", _SCHEMA, True, _echo, destructive=True)
FAILS = ToolSpec("fails", "fails", _SCHEMA, True, _fail)
CRASHES = ToolSpec("crashes", "crashes", _SCHEMA, True, _crash)
UNSERIALISABLE = ToolSpec("unserialisable", "returns a set", _SCHEMA, False, lambda a, c: {"x": {1, 2}})
NOT_A_NUMBER = ToolSpec("not_a_number", "returns nan", _SCHEMA, False, lambda a, c: {"x": float("nan")})
PAIRS = ToolSpec("pairs", "returns a list of pairs", _SCHEMA, False, lambda a, c: [["k", "v"]])
NOTHING = ToolSpec("nothing", "returns None", _SCHEMA, False, lambda a, c: None)
TOOLS = [READ, WRITE, FAILS, CRASHES, UNSERIALISABLE, NOT_A_NUMBER, PAIRS, NOTHING]


def _rpc(method, params=None, id=1):
    message = {"jsonrpc": "2.0", "method": method, "id": id}
    return message if params is None else {**message, "params": params}


def _call(name, arguments):
    return handle(_rpc("tools/call", {"name": name, "arguments": arguments}), TOOLS, "pat")


def test_initialize_reply_shape():
    reply = handle(_rpc("initialize", {"protocolVersion": "2025-03-26"}), TOOLS, "pat")
    assert reply == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "coxswain", "version": SERVER_VERSION},
        },
    }


def test_initialize_unsupported_version_gets_newest():
    reply = handle(_rpc("initialize", {"protocolVersion": "1999-01-01"}), TOOLS, "pat")
    assert reply["result"]["protocolVersion"] == "2025-06-18"


def test_initialized_notification_returns_none():
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, TOOLS, "pat") is None


def test_initialized_with_an_id_is_a_request_and_gets_a_reply():
    reply = handle(_rpc("notifications/initialized", id=7), TOOLS, "pat")
    assert reply == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_notification_without_id_returns_none():
    assert handle({"jsonrpc": "2.0", "method": "no/such"}, TOOLS, "pat") is None


def test_ping_returns_empty_result():
    assert handle(_rpc("ping"), TOOLS, "pat") == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_tools_list_annotations_for_read_and_write_tool():
    listed = handle(_rpc("tools/list"), [READ, WRITE], "pat")["result"]["tools"]
    assert listed[0] == {
        "name": "read_it",
        "description": "reads",
        "inputSchema": _SCHEMA,
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    }
    assert listed[1]["annotations"] == {"readOnlyHint": False, "destructiveHint": True}


def test_call_success_carries_text_and_structured_content():
    result = _call("read_it", {"mode": "fast"})["result"]
    expected = {"mode": "fast", "caller": "pat"}
    assert result == {
        "content": [{"type": "text", "text": json.dumps(expected)}],
        "structuredContent": expected,
        "isError": False,
    }


def test_tool_error_becomes_is_error_true():
    result = _call("fails", {"mode": "fast"})["result"]
    assert result["isError"] is True
    assert result["content"] == [{"type": "text", "text": "lane is frozen"}]


def test_unexpected_exception_text_is_not_echoed():
    reply = _call("crashes", {"mode": "fast"})
    assert reply["result"]["isError"] is True
    assert "secret-detail" not in encode(reply)


def test_unknown_method_is_32601():
    assert handle(_rpc("no/such"), TOOLS, "pat")["error"]["code"] == -32601


def test_malformed_request_echoes_a_readable_id():
    wrong_version = handle({"jsonrpc": "1.0", "method": "ping", "id": 3}, TOOLS, "pat")
    assert (wrong_version["error"]["code"], wrong_version["id"]) == (-32600, 3)
    no_method = handle({"jsonrpc": "2.0", "id": 4}, TOOLS, "pat")
    assert (no_method["error"]["code"], no_method["id"]) == (-32600, 4)


def test_malformed_request_with_unusable_id_gets_null_id():
    not_an_object = handle(["ping"], TOOLS, "pat")
    assert not_an_object == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "request must be an object"},
    }
    bad_id = handle({"jsonrpc": "2.0", "method": "ping", "id": [1], "params": 5}, TOOLS, "pat")
    assert (bad_id["error"]["code"], bad_id["id"]) == (-32600, None)


def test_unknown_tool_is_32602():
    assert _call("missing", {})["error"]["code"] == -32602


def test_non_string_tool_name_is_32602():
    assert _call(["read_it"], {"mode": "fast"})["error"]["code"] == -32602


def test_null_arguments_are_32602():
    assert _call("read_it", None)["error"]["code"] == -32602


def test_notification_with_malformed_params_is_still_not_answered():
    assert handle({"jsonrpc": "2.0", "method": "ping", "params": 5}, TOOLS, "pat") is None


def test_result_that_is_not_json_safe_or_not_a_dict_becomes_is_error_true():
    for name in ("unserialisable", "not_a_number", "pairs", "nothing"):
        reply = _call(name, {"mode": "fast"})
        assert reply["result"]["isError"] is True
        assert "structuredContent" not in reply["result"]


def test_invalid_arguments_are_32602():
    reply = _call("read_it", {"mode": "sideways"})
    assert reply["error"]["code"] == -32602
    assert "mode" in reply["error"]["message"]


def test_validate_missing_required_key():
    assert validate_arguments(_SCHEMA, {}) == ["missing required argument: mode"]


def test_validate_bad_enum_value():
    assert validate_arguments(_SCHEMA, {"mode": "sideways"}) == ["argument mode must be one of ['fast', 'slow']"]


def test_validate_types_and_extra_keys():
    assert validate_arguments(_SCHEMA, {"mode": "fast", "count": True, "extra": 1}) == [
        "unknown argument: extra",
        "argument count must be of type integer",
    ]


def test_decode_invalid_json_is_parse_error():
    reply = decode("{not json")
    assert reply["error"]["code"] == -32700
    assert reply["id"] is None


def test_decode_blank_line_is_none():
    assert decode("  ") is None


def test_decode_valid_line_returns_message():
    assert decode('{"jsonrpc":"2.0","method":"ping","id":1}') == _rpc("ping")


def test_encode_is_one_line():
    line = encode(handle(_rpc("tools/list"), [READ], "pat"))
    assert "\n" not in line
    assert json.loads(line)["id"] == 1
