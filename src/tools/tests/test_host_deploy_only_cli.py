from __future__ import annotations

import argparse

from agent_tools import cli, host_cmd


def _args(tmp_path, monkeypatch, reply):
    (tmp_path / "ws").mkdir(exist_ok=True)
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {tmp_path / 'ws'}\n", encoding="utf-8")
    monkeypatch.delenv("COX_SESSION_LABEL", raising=False)
    seen: list[list[str]] = []
    monkeypatch.setattr(cli.store_cli, "runner", lambda runs_dir: lambda argv: seen.append(argv) or reply)
    return argparse.Namespace(profile=str(profile), name="host-a"), seen


def test_cox_host_deploy_only_sends_state_deploy_only_for_the_named_host(tmp_path, monkeypatch):
    args, seen = _args(tmp_path, monkeypatch, (0, "{}"))
    assert cli._host_deploy_only(args) == 0
    assert seen == [host_cmd.set_state_argv("host-a", "deploy_only", "unlabeled")]
    assert seen[0][:4] == ["host", "set-state", "host-a", "deploy_only"]


def test_cox_host_deploy_only_exits_1_and_prints_a_store_refusal(tmp_path, monkeypatch, capsys):
    args, _ = _args(tmp_path, monkeypatch, (3, "refused: no such host"))
    assert cli._host_deploy_only(args) == 1
    assert capsys.readouterr().out == "host: store_cli exit 3: refused: no such host\n"


def test_deploy_only_is_registered_in_the_host_group_with_its_help():
    entry = next(c for c in cli.HOST_COMMANDS if c.name == "deploy-only")
    assert (entry.summary, entry.handler) == ("receive releases but never lanes", "agent_tools.cli:_host_deploy_only")
