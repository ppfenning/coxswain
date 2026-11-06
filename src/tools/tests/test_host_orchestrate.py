from __future__ import annotations

import argparse

from agent_tools import cli

CARTRIDGE = "policy:\n  dispatch:\n    max_in_flight: 2\n"
REFUSAL = "orchestration needs another active lane host"


def _row(name: str) -> dict:
    return {"name": name, "ssh": name, "capacity": 4, "state": "active", "beat_at": None}


def _setup(tmp_path, monkeypatch, host_names):
    """A profile naming a temp workspace and a temp team cartridge, with a hosts table of `host_names`."""
    (tmp_path / "ws").mkdir()
    cartridge_dir = tmp_path / "cartridges" / "example-team"
    cartridge_dir.mkdir(parents=True)
    cartridge = cartridge_dir / "cartridge.yaml"
    cartridge.write_text(CARTRIDGE, encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        f"workspace_dir: {tmp_path / 'ws'}\nteam: example-team\ncartridges_dir: {tmp_path / 'cartridges'}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.run_store, "hosts", lambda runs_dir: [_row(n) for n in host_names])
    monkeypatch.setattr(cli.socket, "gethostname", lambda: "chair")
    return profile, cartridge


def _args(profile, mode):
    return argparse.Namespace(profile=str(profile), mode=mode)


def test_on_is_refused_on_a_one_host_table_and_writes_nothing(tmp_path, monkeypatch, capsys):
    profile, cartridge = _setup(tmp_path, monkeypatch, ["chair"])
    assert cli._host_orchestrate(_args(profile, "on")) == 1
    assert capsys.readouterr().out == REFUSAL + "\n"
    assert cartridge.read_text(encoding="utf-8") == CARTRIDGE


def test_on_writes_decompose_on_a_three_host_table(tmp_path, monkeypatch):
    profile, cartridge = _setup(tmp_path, monkeypatch, ["chair", "host-a", "host-b"])
    assert cli._host_orchestrate(_args(profile, "on")) == 0
    assert "local_lanes: decompose" in cartridge.read_text(encoding="utf-8")


def test_off_writes_any_even_on_a_one_host_table(tmp_path, monkeypatch):
    profile, cartridge = _setup(tmp_path, monkeypatch, ["chair"])
    assert cli._host_orchestrate(_args(profile, "off")) == 0
    assert "local_lanes: any" in cartridge.read_text(encoding="utf-8")


def test_no_argument_prints_building_unavailable_on_a_one_host_table(tmp_path, monkeypatch, capsys):
    profile, _ = _setup(tmp_path, monkeypatch, ["chair"])
    assert cli._host_orchestrate(_args(profile, None)) == 0
    assert capsys.readouterr().out == "chair: building (unavailable)\n"


def test_no_argument_prints_the_configured_mode_and_availability_on_a_three_host_table(tmp_path, monkeypatch, capsys):
    profile, _ = _setup(tmp_path, monkeypatch, ["chair", "host-a", "host-b"])
    assert cli._host_orchestrate(_args(profile, None)) == 0
    assert capsys.readouterr().out == "chair: building (available)\n"
    assert cli._host_orchestrate(_args(profile, "on")) == 0
    capsys.readouterr()
    assert cli._host_orchestrate(_args(profile, None)) == 0
    assert capsys.readouterr().out == "chair: orchestrating (available)\n"
