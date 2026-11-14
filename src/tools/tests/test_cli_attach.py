"""Bare `cox` attaches to a live chair service over MCP, and starts the workspace session when there is none."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

import pytest

from agent_tools import chair_attach, chair_resolve, cli

FIXED_ID = "conv-fixed-0001"
SERVICE = chair_resolve.SessionHolder("loop", label="chair-service", is_service=True, host="chair-host")


def ns(profile_path: Path, **overrides) -> argparse.Namespace:
    base = {"launcher_profile": str(profile_path), "no_plugin": True, "print_argv": True, "workspace_session": False}
    return argparse.Namespace(**{**base, **overrides})


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A profile and workspace, a state home of its own, claude on the PATH, and spies on everything a take would call."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {workspace}\n", encoding="utf-8")
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/claude")
    calls: dict[str, list] = {"take": [], "takeover": [], "spawn": [], "exec": []}
    monkeypatch.setattr(cli, "_route_chair_take", lambda a: calls["take"].append(a) or 0)
    monkeypatch.setattr(cli.chair_resolve, "takeover", lambda *a, **k: calls["takeover"].append(a) or (True, "taken"))
    monkeypatch.setattr(cli, "_spawn", lambda argv: calls["spawn"].append(argv) or None)
    monkeypatch.setattr(cli.os, "execvp", lambda name, argv: calls["exec"].append(argv))
    holder: list = [SERVICE]
    monkeypatch.setattr(cli, "_live_loop_holder", lambda runs_dir: holder[0])
    stored = chair_attach.conversation_path(str(state_home), tmp_path)
    return profile, workspace, calls, holder, stored


def printed(capsys) -> tuple[list[str], str, str]:
    out = capsys.readouterr()
    argv_line, cwd_line = out.out.strip().splitlines()[-2:]
    return ast.literal_eval(argv_line), cwd_line, out.err


def test_with_a_live_chair_of_another_label_the_session_attaches_over_mcp_and_touches_no_lease(rig, capsys, monkeypatch, tmp_path) -> None:
    profile, workspace, calls, _holder, stored = rig
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert cli._launcher(ns(profile), ["-p", "hello"], mint=lambda: FIXED_ID) == 0
    argv, cwd, err = printed(capsys)
    assert argv[0] == "claude" and argv[-2:] == ["-p", "hello"], "the operator's own arguments stay last"
    config = json.loads(argv[argv.index("--mcp-config") + 1])
    server = config["mcpServers"]["coxswain-chair"]
    assert (server["command"], server["args"]) == ("cox", ["mcp"])
    assert server["env"] == {"COXSWAIN_MCP_CONVERSATION": FIXED_ID}
    assert "--resume" not in argv and "-r" not in argv[:-2]
    assert cwd == str(elsewhere), "it works from wherever it was started, not from the workspace"
    assert "attached to it over MCP" in err
    assert calls == {"take": [], "takeover": [], "spawn": [], "exec": []}
    assert not (workspace / "runs").exists(), "nothing was written to the workspace"
    assert chair_attach.read_conversation(stored.read_text()) == FIXED_ID, "a minted id is stored for the next session"


def test_a_stored_conversation_id_is_used_and_left_as_it_was(rig, capsys) -> None:
    profile, _workspace, _calls, _holder, stored = rig
    stored.parent.mkdir(parents=True)
    stored.write_text(chair_attach.conversation_text("conv-earlier"))
    before = stored.read_bytes()
    assert cli._launcher(ns(profile), [], mint=lambda: pytest.fail("an id was minted over a stored one")) == 0
    argv, _cwd, _err = printed(capsys)
    env = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]["coxswain-chair"]["env"]
    assert env["COXSWAIN_MCP_CONVERSATION"] == "conv-earlier" and stored.read_bytes() == before


def test_with_no_live_chair_the_workspace_session_is_built_as_before(rig, capsys) -> None:
    profile, workspace, calls, holder, stored = rig
    holder[0] = None
    assert cli._launcher(ns(profile), ["-p", "hello"], mint=lambda: pytest.fail("no attach, so no id")) == 0
    argv, cwd, _err = printed(capsys)
    assert argv == ["claude", "-p", "hello"] and cwd == str(workspace)
    assert "--mcp-config" not in argv and not stored.exists()
    assert calls["exec"] == []


def test_the_workspace_flag_forces_the_workspace_session_under_a_live_chair(rig, capsys) -> None:
    profile, workspace, _calls, _holder, stored = rig
    assert cli._launcher(ns(profile, workspace_session=True), [], mint=lambda: pytest.fail("forced to the workspace")) == 0
    argv, cwd, _err = printed(capsys)
    assert "--mcp-config" not in argv and cwd == str(workspace) and not stored.exists()


def test_a_chair_loop_under_this_session_s_own_label_is_not_attached_to(rig, capsys) -> None:
    import datetime

    profile, workspace, _calls, holder, _stored = rig
    holder[0] = chair_resolve.SessionHolder("loop", label=f"chair-{datetime.datetime.now(datetime.UTC):%Y-%m-%d}")
    assert cli._launcher(ns(profile), []) == 0
    argv, cwd, _err = printed(capsys)
    assert "--mcp-config" not in argv and cwd == str(workspace)


def test_the_plugin_directory_rides_on_the_attached_session_unless_no_plugin(rig, capsys, monkeypatch, tmp_path) -> None:
    profile, _workspace, _calls, _holder, _stored = rig
    plugin = tmp_path / "plugins" / "coxswain"
    monkeypatch.setattr(cli, "_plugin_root", lambda roots: plugin)
    assert cli._launcher(ns(profile, no_plugin=False), [], mint=lambda: FIXED_ID) == 0
    argv, _cwd, _err = printed(capsys)
    assert argv[argv.index("--plugin-dir") + 1] == str(plugin)
    assert cli._launcher(ns(profile, no_plugin=True), [], mint=lambda: FIXED_ID) == 0
    assert "--plugin-dir" not in printed(capsys)[0]


def test_attaching_for_real_execs_claude_where_it_stands_and_hands_the_server_the_profile(rig, capsys, monkeypatch, tmp_path) -> None:
    profile, _workspace, calls, _holder, _stored = rig
    monkeypatch.delenv("AGENT_TOOLS_PROFILE", raising=False)
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", "unset-before")
    moved: list = []
    monkeypatch.setattr(cli.os, "chdir", moved.append)
    assert cli._launcher(ns(profile, print_argv=False), [], mint=lambda: FIXED_ID) == 0
    (argv,) = calls["exec"]
    assert argv[0] == "claude" and "--mcp-config" in argv and moved == []
    assert cli.os.environ["AGENT_TOOLS_PROFILE"] == str(profile)
    assert calls["take"] == [] and calls["takeover"] == [] and calls["spawn"] == []
    assert "attaching to it over MCP" in capsys.readouterr().out


def test_a_missing_profile_still_returns_its_own_exit_code(tmp_path, capsys) -> None:
    assert cli._launcher(ns(tmp_path / "none.yaml"), []) == 2


def test_the_parser_names_the_flag_on_both_entry_points_and_says_which_mode_runs() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["--workspace"]).workspace_session is True
    assert parser.parse_args([]).workspace_session is False
    assert parser.parse_args(["session", "--workspace"]).session_workspace is True
    text = parser.format_help()
    assert "attached over MCP to a live chair service" in text and "--workspace" in text


def test_bare_cox_mcp_serves_as_the_attached_session_s_config_asks(tmp_path, monkeypatch) -> None:
    import io

    ws = tmp_path / "ws"
    (ws / "runs").mkdir(parents=True)
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {ws}\n", encoding="utf-8")
    monkeypatch.setenv("COXSWAIN_MCP_CONVERSATION", "conv-earlier")
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "cut_release", "arguments": {"version": "2026.11.7", "confirm": "2026.11.7"}}}
    out = io.StringIO()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request) + "\n"))
    monkeypatch.setattr("sys.stdout", out)
    server = chair_attach.mcp_config("cox", "conv-earlier")["mcpServers"]["coxswain-chair"]
    assert cli.main([*server["args"], "--profile", str(profile)]) == 0
    assert json.loads(out.getvalue())["result"]["structuredContent"]["status"] == "sent"
    (entry,) = [json.loads(line) for line in (ws / "courier.jsonl").read_text().splitlines()]
    assert entry["from"] == "conv-earlier", "every entry the session sends carries its conversation id"
