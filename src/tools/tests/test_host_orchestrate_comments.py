from __future__ import annotations

import argparse

from agent_tools import cli

CARTRIDGE = """\
# Team cartridge for the example team.
# Edited by hand; keep this header.

policy:
  dispatch:
    max_in_flight: 2  # lanes at once
    local_lanes: any  # any | decompose

  # between policy sections
  review:
    required: true  # never skipped

# closing note
"""
HOSTS = ["chair", "host-a", "host-b"]


def _row(name: str) -> dict:
    return {"name": name, "ssh": name, "capacity": 4, "state": "active", "beat_at": None}


def _setup(tmp_path, monkeypatch):
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
    monkeypatch.setattr(cli.run_store, "hosts", lambda runs_dir: [_row(n) for n in HOSTS])
    monkeypatch.setattr(cli.socket, "gethostname", lambda: "chair")
    return profile, cartridge


def _run(profile, mode) -> int:
    return cli._host_orchestrate(argparse.Namespace(profile=str(profile), mode=mode))


def _value_line(mode_value: str) -> str:
    return f"    local_lanes: {mode_value}  # any | decompose"


def _lines_but_value(text: str) -> list[str]:
    return [ln for ln in text.split("\n") if "local_lanes:" not in ln]


def test_each_mode_changes_only_the_local_lanes_value_line(tmp_path, monkeypatch):
    profile, cartridge = _setup(tmp_path, monkeypatch)
    for mode, value in [("on", "decompose"), ("off", "any"), ("on", "decompose")]:
        assert _run(profile, mode) == 0
        text = cartridge.read_text(encoding="utf-8")
        assert _lines_but_value(text) == _lines_but_value(CARTRIDGE)
        assert [ln for ln in text.split("\n") if "local_lanes:" in ln] == [_value_line(value)]
        assert len(text.split("\n")) == len(CARTRIDGE.split("\n"))


def test_a_repeated_mode_leaves_the_file_byte_identical_and_writes_nothing(tmp_path, monkeypatch, capsys):
    profile, cartridge = _setup(tmp_path, monkeypatch)
    for mode in ("on", "off"):
        assert _run(profile, mode) == 0
        before, mtime = cartridge.read_bytes(), cartridge.stat().st_mtime_ns
        capsys.readouterr()
        assert _run(profile, mode) == 0
        out = capsys.readouterr().out
        assert "no change:" in out
        assert "wrote" not in out
        assert cartridge.read_bytes() == before
        assert cartridge.stat().st_mtime_ns == mtime
