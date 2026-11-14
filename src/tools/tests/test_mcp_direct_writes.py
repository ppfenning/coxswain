"""MCP writes act and report what happened; only what needs the chair is sent to it. Cartridge tools included."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_tools import mcp_actions
from agent_tools.mcp_protocol import ToolError
from agent_tools.mcp_read_tools import ReadPorts, cartridge_settings
from agent_tools.mcp_write_tools import write_tools

NOW = "2026-11-12T10:00:00Z"
CALLER = "conv-1"


class Bus:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    def __call__(self, event: str, fields: dict) -> str:
        self.sent.append((event, fields))
        return f"m{len(self.sent)}"


class Actor:
    def __init__(self, ok: bool = True, detail: str = "ok") -> None:
        self.calls: list[tuple[str, dict]] = []
        self.ok, self.detail = ok, detail

    def __call__(self, tool: str, args: dict) -> tuple[bool, str]:
        self.calls.append((tool, dict(args)))
        return self.ok, self.detail


def call(name: str, arguments: dict, act: Actor | None) -> tuple[Bus, dict]:
    bus = Bus()
    tools = {t.name: t for t in write_tools(bus, NOW, known_repos=["core"], known_epics=["mcp"], act=act)}
    return bus, tools[name].run(arguments, CALLER)


def test_a_plain_write_is_carried_out_and_recorded_not_requested() -> None:
    act = Actor(detail="route: add-a-thing is ready")
    bus, result = call("set_stage", {"initiative": "add-a-thing", "stage": "ready"}, act)
    assert act.calls == [("set_stage", {"initiative": "add-a-thing", "stage": "ready", "caller": CALLER})]
    assert (result["status"], result["detail"]) == ("done", "route: add-a-thing is ready")
    ((event, entry),) = bus.sent
    assert event == "chat_action", "a record of what was done, which nothing acts on again"
    assert (entry["tool"], entry["status"], entry["initiative"], entry["caller"]) == ("set_stage", "done", "add-a-thing", CALLER)


def test_a_refused_write_says_why_and_is_recorded_as_refused() -> None:
    bus, result = call("set_priority", {"initiative": "gone", "priority": 5}, Actor(False, "route priority: no open item gone"))
    assert (result["status"], result["detail"]) == ("refused", "route priority: no open item gone")
    assert bus.sent[0][1]["status"] == "refused"


def test_filing_holding_and_releasing_act_directly() -> None:
    act = Actor()
    _, filed = call("file_intake", {"repo": "core", "title": "Add a thing", "priority": "3"}, act)
    _, held = call("send_directive", {"kind": "hold", "initiative": "add-a-thing"}, act)
    _, freed = call("send_directive", {"kind": "release", "initiative": "add-a-thing"}, act)
    assert [filed["status"], held["status"], freed["status"]] == ["done", "done", "done"]
    assert act.calls[0][1]["title"] == "Add a thing" and act.calls[0][1]["stage"] == "draft"
    assert act.calls[1][1]["reason"] == f"held over MCP by {CALLER}", "a hold always says why"
    assert "reason" not in act.calls[2][1]


def test_what_needs_the_chair_is_still_sent_to_it() -> None:
    act = Actor()
    bus, scoped = call("send_directive", {"kind": "scope", "initiative": "add-a-thing"}, act)
    assert scoped["status"] == "sent" and bus.sent[0][0] == "chat_directive"
    bus, cut = call("cut_release", {"version": "2026.11.7", "confirm": "2026.11.7"}, act)
    assert cut["status"] == "sent" and bus.sent[0][0] == "chat_directive"
    _, unconfirmed = call("cut_release", {"version": "2026.11.7"}, act)
    assert unconfirmed["status"] == "needs_confirm"
    assert act.calls == [], "neither is carried out by the server"


def test_a_server_with_no_actor_asks_the_chair_as_before() -> None:
    bus, result = call("set_stage", {"initiative": "add-a-thing", "stage": "ready"}, None)
    assert result["status"] == "sent" and bus.sent[0][0] == "chat_directive" and "detail" not in result


def test_a_setting_shows_its_diff_first_and_writes_only_when_confirmed() -> None:
    act = Actor(detail="-max_in_flight: 2\n+max_in_flight: 3")
    bus, shown = call("set_setting", {"key": "policy.max_in_flight", "value": "3"}, act)
    assert shown["status"] == "needs_confirm" and "+max_in_flight: 3" in shown["detail"]
    assert act.calls == [("set_setting", {"key": "policy.max_in_flight", "value": "3", "dry_run": True, "caller": CALLER})]
    assert bus.sent == [], "a preview is not an event"
    bus, written = call("set_setting", {"key": "policy.max_in_flight", "value": "3", "confirm": True}, act)
    assert written["status"] == "done" and "dry_run" not in act.calls[-1][1]
    assert bus.sent[0][0] == "chat_action"


def test_a_setting_cannot_be_changed_or_read_away_from_the_cartridge() -> None:
    with pytest.raises(ToolError, match="does not run on the machine that holds it"):
        call("set_setting", {"key": "policy.max_in_flight", "value": "3", "confirm": True}, None)
    away = ReadPorts(snapshot=dict, release=dict, unlaunched=lambda ident: [], events=lambda since, limit: [])
    with pytest.raises(ToolError, match="cannot read the cartridge"):
        cartridge_settings({}, away)


def _settings() -> dict:
    return {"sections": [
        {"name": "builds and budgets", "rows": [
            {"scope": "cartridge", "key": "policy.max_in_flight", "value": 2, "source_file": "cartridge.yaml", "tracked": True, "operator_only": False},
            {"scope": "cartridge", "key": "policy.gate", "value": "full", "source_file": "cartridge.yaml", "tracked": True, "operator_only": True},
        ]},
        {"name": "housekeeping", "rows": []},
    ]}


def test_the_settings_read_is_compact_until_asked_for_detail() -> None:
    ports = ReadPorts(snapshot=dict, release=dict, unlaunched=lambda ident: [], events=lambda since, limit: [], settings=_settings)
    assert cartridge_settings({}, ports) == {"sections": [{"name": "builds and budgets", "settings": 2}, {"name": "housekeeping", "settings": 0}]}
    rows = cartridge_settings({"section": "builds and budgets"}, ports)["settings"]
    assert [r["key"] for r in rows] == ["policy.max_in_flight", "policy.gate"] and rows[1]["operator_only"] is True
    (one,) = cartridge_settings({"key": "policy.max_in_flight"}, ports)["settings"]
    assert (one["value"], one["source_file"], one["section"]) == (2, "cartridge.yaml", "builds and budgets")
    with pytest.raises(ToolError, match="unknown section"):
        cartridge_settings({"section": "nope"}, ports)
    with pytest.raises(ToolError, match="unknown setting"):
        cartridge_settings({"key": "nope"}, ports)


def test_each_write_is_spelled_as_the_command_a_person_would_type() -> None:
    argv = mcp_actions.argv_for
    assert argv("set_stage", {"initiative": "x", "stage": "ready"}) == ["route", "ready", "x"]
    assert argv("set_priority", {"initiative": "x", "priority": 7}) == ["route", "priority", "x", "--set", "7"]
    assert argv("send_directive", {"kind": "hold", "initiative": "x", "reason": "wait", "caller": "conv-1"}) == [
        "route", "hold", "x", "--reason", "wait", "--by", "conv-1"]
    assert argv("send_directive", {"kind": "release", "initiative": "x"}) == ["route", "release", "x"]
    assert argv("file_intake", {"repo": "core", "title": "Add a thing", "stage": "ready", "epic": "mcp", "priority": 3}) == [
        "route", "file", "--intake", "--repo", "core", "--title", "Add a thing", "--ready", "--epic", "mcp", "--priority", "3"]
    assert argv("set_setting", {"key": "k", "value": "v", "dry_run": True}) == ["settings", "set", "cartridge", "k", "v", "--dry-run"]
    assert argv("set_setting", {"key": "k", "value": "v"}) == ["settings", "set", "cartridge", "k", "v", "--commit"]
    with pytest.raises(mcp_actions.Refused):
        argv("send_directive", {"kind": "scope", "initiative": "x"})
    with pytest.raises(mcp_actions.Refused):
        argv("cut_release", {"version": "1"})


def test_the_actor_runs_the_command_and_returns_its_exit_and_output(tmp_path: Path, monkeypatch) -> None:
    ran: list[list[str]] = []

    def run(argv: list[str]) -> tuple[int, str]:
        ran.append(argv)
        return 0, "route: x is ready\n"

    act = mcp_actions.local_actor(tmp_path / "profile.yaml", run)
    assert act("set_stage", {"initiative": "x", "stage": "ready"}) == (True, "route: x is ready")
    assert ran == [["route", "ready", "x", "--profile", str(tmp_path / "profile.yaml")]]
    assert act("cut_release", {"version": "1"})[0] is False and len(ran) == 1, "nothing runs for a tool with no command"
    refused = mcp_actions.local_actor(tmp_path / "p.yaml", lambda argv: (2, "route ready: no queued intake x\n"))
    assert refused("set_stage", {"initiative": "x", "stage": "ready"}) == (False, "route ready: no queued intake x")

    def boom(argv: list[str]) -> tuple[int, str]:
        raise RuntimeError("store down")

    assert mcp_actions.local_actor(tmp_path / "p.yaml", boom)("set_stage", {"initiative": "x", "stage": "ready"}) == (False, "RuntimeError: store down")
    monkeypatch.setattr(mcp_actions, "operator_only", lambda key: key == "policy.gate")
    ok, why = act("set_setting", {"key": "policy.gate", "value": "none"})
    assert not ok and "operator-only" in why and len(ran) == 1, "an operator-only setting never reaches the command"
