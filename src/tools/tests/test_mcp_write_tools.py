import json

import pytest

from agent_tools.mcp_protocol import ToolError, handle
from agent_tools.mcp_write_tools import write_tools

NOW = "2026-11-10T09:00:00Z"
CALLER = "conv-7"


class Recorder:
    def __init__(self):
        self.sent = []

    def __call__(self, event, fields):
        self.sent.append((event, fields))
        return f"m{len(self.sent)}"


def _tools(send):
    specs = write_tools(send, NOW, known_repos=["core", "dash"], known_epics=["mcp"])
    return {spec.name: spec for spec in specs}


def _call(name, arguments):
    send = Recorder()
    result = _tools(send)[name].run(arguments, CALLER)
    return send, result


def test_each_tool_sends_one_entry_carrying_the_caller():
    calls = {
        "file_intake": {
            "repo": "core",
            "epic": "mcp",
            "title": "Add x",
            "priority": "3",
            "target_release": "v2026.11.6",
        },
        "set_stage": {"initiative": "init-1", "stage": "ready"},
        "set_priority": {"initiative": "init-1", "priority": 5},
        "send_directive": {"kind": "enable", "feature": "mcp"},
        "cut_release": {"version": "2026.11.7", "confirm": "2026.11.7"},
    }
    for name, arguments in calls.items():
        send, result = _call(name, arguments)
        assert len(send.sent) == 1, name
        event, fields = send.sent[0]
        assert event == "chat_directive"
        assert (fields["caller"], fields["conversation"], fields["tool"], fields["at"]) == (CALLER, CALLER, name, NOW)
        assert result["status"] == "sent"
        assert result["message_id"] == "m1"
        assert result["will_change"].endswith(".")


def test_file_intake_entry_carries_normalised_arguments():
    send, _ = _call(
        "file_intake", {"repo": " core ", "title": "Add x", "priority": "3", "target_release": "v2026.11.6"}
    )
    fields = send.sent[0][1]
    assert (fields["repo"], fields["priority"], fields["target_release"]) == ("core", 3, "2026.11.6")
    assert (fields["title"], fields["stage"]) == ("Add x", "draft")


def test_file_intake_refusals_send_nothing_and_list_every_one():
    send = Recorder()
    run = _tools(send)["file_intake"].run
    with pytest.raises(ToolError) as bad:
        run({"repo": "nope", "title": "t", "priority": "high"}, CALLER)
    assert "repo 'nope'" in str(bad.value)
    assert "priority 'high'" in str(bad.value)
    with pytest.raises(ToolError):
        run({"repo": "core", "title": "t", "priority": "high"}, CALLER)
    with pytest.raises(ToolError):
        run({"repo": "core", "title": "t", "stage": "done"}, CALLER)
    with pytest.raises(ToolError):
        run({"repo": "core", "title": " "}, CALLER)
    assert send.sent == []


def test_other_refusals_send_nothing():
    cases = [
        ("set_stage", {"initiative": "i", "stage": "done"}),
        ("set_stage", {"initiative": " ", "stage": "draft"}),
        ("set_priority", {"initiative": "i", "priority": "urgent"}),
        ("set_priority", {"initiative": "i", "priority": True}),
        ("send_directive", {"kind": "hold"}),
        ("send_directive", {"kind": "enable", "initiative": "i"}),
        ("send_directive", {"kind": "wipe", "initiative": "i"}),
    ]
    for name, arguments in cases:
        send = Recorder()
        with pytest.raises(ToolError):
            _tools(send)[name].run(arguments, CALLER)
        assert send.sent == [], (name, arguments)


def test_cut_release_needs_a_matching_confirm():
    for confirm in ({}, {"confirm": ""}, {"confirm": "2026.11.6"}):
        send, result = _call("cut_release", {"version": "2026.11.7", **confirm})
        assert send.sent == []
        assert result["status"] == "needs_confirm"
        assert result["message_id"] == ""
        assert "Cut release 2026.11.7." in result["will_change"]
    send, result = _call("cut_release", {"version": "2026.11.7", "confirm": "2026.11.7"})
    assert len(send.sent) == 1
    assert result["status"] == "sent"


def test_specs_report_their_safety_hints():
    tools = _tools(Recorder())
    assert sorted(tools) == ["cut_release", "file_intake", "send_directive", "set_priority", "set_stage"]
    assert all(spec.writes for spec in tools.values())
    assert {name for name, spec in tools.items() if spec.destructive} == {"cut_release"}


def test_protocol_reports_a_refused_call_as_a_tool_error_and_sends_nothing():
    send = Recorder()
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "file_intake", "arguments": {"repo": "nope", "title": "t"}},
    }
    reply = handle(message, list(_tools(send).values()), CALLER)
    assert reply["result"]["isError"] is True
    assert "repo 'nope'" in reply["result"]["content"][0]["text"]
    assert send.sent == []


def test_protocol_passes_the_caller_into_the_entry():
    send = Recorder()
    message = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "set_stage", "arguments": {"initiative": "i", "stage": "ready"}},
    }
    reply = handle(message, list(_tools(send).values()), CALLER)
    assert reply["result"]["isError"] is False
    assert json.loads(reply["result"]["content"][0]["text"])["message_id"] == "m1"
    assert send.sent[0][1]["conversation"] == CALLER
