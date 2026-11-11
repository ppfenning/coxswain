import json

import pytest
from agent_tools.mcp_read_tools import ReadPorts

from agent_tools import mcp_server
from agent_tools.mcp_protocol import INVALID_REQUEST, PARSE_ERROR, ToolSpec, handle
from agent_tools.mcp_server import Ports, build_tools, make_handler, serve_stdio

NOW = "2026-11-11T09:00:00Z"
CALLER = "stdio-1"
READ_NAMES = ["release_status", "queue_board", "explain", "recent_events"]
WRITE_NAMES = ["file_intake", "set_stage", "set_priority", "send_directive", "cut_release"]
SNAPSHOT = {"board": {"epics": [{"initiatives": [{"id": "mcp", "counts": {"merged": 2, "ready": 1}}]}]}}


class Recorder:
    def __init__(self):
        self.sent = []

    def __call__(self, event, fields):
        self.sent.append((event, fields))
        return f"m{len(self.sent)}"


def _ports(send):
    read = ReadPorts(
        snapshot=lambda: SNAPSHOT,
        release=lambda: {"version": "v1"},
        unlaunched=lambda _initiative: [],
        events=lambda _kind, _limit: [],
    )
    return Ports(read, send, lambda: NOW, known_repos=["core"], known_epics=["mcp"])


def _request(request_id, method, params=None):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    return json.dumps(message if params is None else {**message, "params": params})


def _session(lines, send=None):
    send = send if send is not None else Recorder()
    written = []
    code = serve_stdio(lines, written.append, make_handler(_ports(send), CALLER))
    return code, written, send


def _responses(written):
    return [json.loads(line) for line in written]


def test_build_tools_has_four_read_then_five_write_tools_without_duplicates():
    names = [tool.name for tool in build_tools(_ports(Recorder()))]
    assert names == [*READ_NAMES, *WRITE_NAMES]
    assert len(set(names)) == len(names)


def test_build_tools_refuses_a_duplicate_name(monkeypatch):
    clash = ToolSpec("set_stage", "clash", {"type": "object"}, False, lambda arguments, caller: {})
    monkeypatch.setattr(mcp_server, "read_tools", lambda _ports: [clash])
    with pytest.raises(ValueError, match="set_stage"):
        build_tools(_ports(Recorder()))


def test_handler_answers_as_the_protocol_core_does():
    ports = _ports(Recorder())
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert make_handler(ports, CALLER)(message) == handle(message, build_tools(ports), CALLER)


def test_scripted_session_yields_exactly_three_response_lines():
    lines = [
        _request(1, "initialize", {"protocolVersion": "2025-06-18"}) + "\n",
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n",
        _request(2, "tools/list") + "\n",
        _request(3, "tools/call", {"name": "release_status", "arguments": {}}) + "\n",
    ]
    code, written, _send = _session(lines)
    responses = _responses(written)
    assert code == 0
    assert all(line.endswith("\n") and line.count("\n") == 1 for line in written)
    assert [r["id"] for r in responses] == [1, 2, 3]
    assert [t["name"] for t in responses[1]["result"]["tools"]] == [*READ_NAMES, *WRITE_NAMES]
    assert responses[2]["result"]["structuredContent"]["totals"] == {"done": 2, "open": 1}


def test_set_priority_makes_the_send_record_one_entry_with_the_caller():
    call = _request(1, "tools/call", {"name": "set_priority", "arguments": {"initiative": "mcp", "priority": 5}})
    _code, written, send = _session([call])
    assert _responses(written)[0]["result"]["isError"] is False
    assert len(send.sent) == 1
    event, entry = send.sent[0]
    assert event == "chat_directive"
    assert entry["caller"] == CALLER
    assert entry["tool"] == "set_priority"


def test_garbage_line_gets_a_parse_error_and_the_next_line_is_answered():
    code, written, _send = _session(["{not json\n", _request(2, "ping") + "\n"])
    first, second = _responses(written)
    assert code == 0
    assert first["error"]["code"] == PARSE_ERROR
    assert second == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_a_json_line_that_is_not_an_object_gets_an_invalid_request_and_the_loop_continues():
    code, written, _send = _session(["[1]\n", _request(2, "ping") + "\n"])
    first, second = _responses(written)
    assert code == 0
    assert first == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": INVALID_REQUEST, "message": first["error"]["message"]},
    }
    assert second["id"] == 2


def test_blank_lines_write_nothing():
    code, written, _send = _session(["\n", "   \n", ""])
    assert (code, written) == (0, [])
