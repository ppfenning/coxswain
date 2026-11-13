"""`cox mcp serve`: the chair's MCP tools, wired to the workspace, the bus and the cox commands."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from agent_tools import chair_chat, cli, courier, mcp_ports
from agent_tools.mcp_protocol import ToolError


def test_a_request_to_the_chair_is_the_directive_its_chat_already_understands() -> None:
    cut = mcp_ports.directive_payload({"tool": "cut_release", "version": "2026.11.7", "confirm": "2026.11.7"})
    assert cut == {"kind": "cut_release", "version": "2026.11.7"}
    scope = mcp_ports.directive_payload({"tool": "send_directive", "kind": "scope", "initiative": "add-a-thing"})
    feature = mcp_ports.directive_payload({"tool": "send_directive", "kind": "enable", "feature": "batching"})
    hold = mcp_ports.directive_payload({"tool": "send_directive", "kind": "hold", "initiative": "add-a-thing"})
    rank = mcp_ports.directive_payload({"tool": "set_priority", "initiative": "add-a-thing", "priority": 3})
    assert feature == {"kind": "feature", "name": "batching", "state": "on"}
    assert rank == {"kind": "priority", "target_kind": "initiative", "target": "add-a-thing", "rank": "3"}
    for payload in (cut, scope, feature, hold, rank):
        assert chair_chat.validate_directive(payload["kind"], payload) is None, payload
    for tool in ("set_stage", "file_intake", "set_setting"):
        with pytest.raises(ToolError, match="needs a server that runs beside the store"):
            mcp_ports.directive_payload({"tool": tool})


def test_a_bus_entry_is_one_the_chair_chat_parses_and_a_done_write_is_never_a_directive() -> None:
    asked = mcp_ports.bus_entry("chat_directive", {"tool": "cut_release", "version": "2026.11.7", "caller": "conv-1"}, "m1")
    message = chair_chat.parse_message(asked)
    assert message is not None and (message["body_kind"], message["sender"], message["payload"]["kind"]) == ("directive", "conv-1", "cut_release")
    done = mcp_ports.bus_entry("chat_action", {"tool": "set_stage", "initiative": "add-a-thing", "stage": "ready", "status": "done",
                                               "caller": "conv-1", "at": "2026-11-12T10:00:00+00:00"}, "m2")
    assert (done["event"], done["ref"], done["to"], done["topic"]) == ("chat_action", "coxswain://initiative/add-a-thing", "chair", chair_chat.LABEL)
    assert all(isinstance(v, str) for v in done["fields"].values())
    assert chair_chat.parse_message(done) is None, "the chair's chat acts on directives and questions only"
    assert "chat_action" in courier.EVENTS and "chat_action" not in courier.EXCEPTION_EVENTS


def test_recent_events_are_the_newest_typed_bus_entries_after_since() -> None:
    lines = [
        {"ref": "coxswain://run/r1", "event": "run_exited", "fields": {"at": "2026-11-12T09:00:00+00:00"}},
        {"ref": "coxswain://task/t1", "note": "no event"},
        {"ref": "coxswain://initiative/i1", "event": "chat_action", "fields": {"at": "2026-11-12T10:00:00+00:00"}},
        {"ref": "coxswain://initiative/i2", "event": "needs_attention", "fields": {"at": "2026-11-12T11:00:00+00:00"}},
    ]
    text = "\n".join([*(json.dumps(line) for line in lines), "not json"])
    assert [e["subject"] for e in mcp_ports.bus_events(text, None, 2)] == ["coxswain://initiative/i1", "coxswain://initiative/i2"]
    assert [e["type"] for e in mcp_ports.bus_events(text, "2026-11-12T10:30:00+00:00", 10)] == ["needs_attention"]
    assert mcp_ports.bus_events("", None, 5) == []


def test_the_chair_s_last_reasons_answer_why_an_initiative_did_not_launch() -> None:
    text = mcp_ports.reasons_text(
        [{"initiative": "add-a-thing", "reason": "no lane free on an eligible host"}, {"initiative": "other", "reason": "held"}],
        "2026-11-12T10:00:00+00:00",
    )
    assert mcp_ports.unlaunched_reasons(text, "add-a-thing") == [{"initiative": "add-a-thing", "reason": "no lane free on an eligible host"}]
    assert mcp_ports.unlaunched_reasons(text, "launched-one") == []
    assert mcp_ports.unlaunched_reasons(None, "add-a-thing") == [] and mcp_ports.unlaunched_reasons("not json", "add-a-thing") == []


def test_a_tick_leaves_its_reasons_for_the_server_and_a_dry_run_leaves_the_file_alone(tmp_path: Path) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from agent_tools import chair_run

    state = chair_run.ReasonState(items=[{"initiative": "add-a-thing", "reason": "no lane free on an eligible host"}])
    deps = SimpleNamespace(runs_dir=tmp_path, reason_state=state)
    now = datetime(2026, 11, 12, 10, tzinfo=UTC)
    chair_run._publish_reasons(deps, True, now)
    assert not (tmp_path / mcp_ports.UNLAUNCHED_FILE).exists()
    chair_run._publish_reasons(deps, False, now)
    held = (tmp_path / mcp_ports.UNLAUNCHED_FILE).read_text()
    assert mcp_ports.unlaunched_reasons(held, "add-a-thing")[0]["reason"] == "no lane free on an eligible host"
    state.items = []
    chair_run._publish_reasons(deps, False, now)
    assert mcp_ports.unlaunched_reasons((tmp_path / mcp_ports.UNLAUNCHED_FILE).read_text(), "add-a-thing") == [], "the next tick replaces them"
    chair_run._publish_reasons(SimpleNamespace(runs_dir=None, reason_state=state), False, now)


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`cox mcp serve` over a workspace of its own, fed requests as lines; returns the decoded responses."""
    ws = tmp_path / "ws"
    (ws / "runs").mkdir(parents=True)
    profile = tmp_path / "profile.yaml"
    profile.write_text(f'workspace_dir: {ws}\nrepo_map: {{"acme/app": "app"}}\n', encoding="utf-8")

    def serve(*requests: dict, argv: tuple[str, ...] = ()) -> tuple[int, list[dict], Path]:
        out = io.StringIO()
        monkeypatch.setattr("sys.stdin", io.StringIO("".join(json.dumps(r) + "\n" for r in requests)))
        monkeypatch.setattr("sys.stdout", out)
        code = cli.main(["mcp", "serve", "--profile", str(profile), *argv])
        return code, [json.loads(line) for line in out.getvalue().splitlines()], ws

    return serve


def _call(ident: int, name: str, arguments: dict) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def test_the_command_serves_every_tool_on_stdin_and_stdout(served) -> None:
    code, responses, _ = served(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert code == 0 and [r["id"] for r in responses] == [1, 2], "only protocol messages reach stdout"
    assert sorted(t["name"] for t in responses[1]["result"]["tools"]) == [
        "cartridge_settings", "cut_release", "explain", "file_intake", "queue_board", "recent_events", "release_status",
        "send_directive", "set_priority", "set_setting", "set_stage",
    ]


def test_a_request_for_the_chair_lands_on_the_bus_as_a_directive_and_shows_in_recent_events(served) -> None:
    code, responses, ws = served(
        _call(1, "cut_release", {"version": "2026.11.7", "confirm": "2026.11.7"}),
        _call(2, "recent_events", {}),
        argv=("--label", "conv-1"),
    )
    assert code == 0
    assert responses[0]["result"]["structuredContent"]["status"] == "sent"
    (entry,) = [json.loads(line) for line in (ws / "courier.jsonl").read_text().splitlines()]
    message = chair_chat.parse_message(entry)
    assert message is not None and (message["sender"], message["payload"]) == ("conv-1", {"kind": "cut_release", "version": "2026.11.7"})
    assert [e["type"] for e in responses[1]["result"]["structuredContent"]["events"]] == ["chat_directive"]


def test_a_plain_write_runs_its_cox_command_and_reports_what_it_said(served, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_tools import mcp_actions

    ran: list[list[str]] = []
    monkeypatch.setattr(mcp_actions, "run_cox", lambda argv: ran.append(argv) or (2, "route ready: no queued intake add-a-thing\n"))
    code, responses, ws = served(
        _call(1, "set_stage", {"initiative": "add-a-thing", "stage": "ready"}),
        _call(2, "file_intake", {"repo": "acme/app", "title": "Add a thing"}),
    )
    result = responses[0]["result"]["structuredContent"]
    assert code == 0 and (result["status"], result["detail"]) == ("refused", "route ready: no queued intake add-a-thing")
    assert ran[0][:3] == ["route", "ready", "add-a-thing"] and ran[0][3] == "--profile"
    assert ran[1][ran[1].index("--repo") + 1] == "app", "a repo named as the forge knows it is passed as the profile maps it"
    first, _second = [json.loads(line) for line in (ws / "courier.jsonl").read_text().splitlines()]
    assert (first["event"], first["fields"]["status"]) == ("chat_action", "refused")


def test_http_needs_an_address_and_a_usable_tokens_file(served, tmp_path: Path, capsys) -> None:
    assert served(argv=("--http", "nonsense"))[0] == 2
    assert served(argv=("--http", "127.0.0.1:8765"))[0] == 2
    short = tmp_path / "tokens"
    short.write_text("client-a tooshort\n")
    assert served(argv=("--http", "127.0.0.1:8765", "--tokens-file", str(short)))[0] == 2
    assert "tokens file cannot be used" in capsys.readouterr().err
