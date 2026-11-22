from __future__ import annotations

import argparse

import yaml

from agent_tools import cli, host_cmd, store_cli
from tests.comment_helpers import _comments, _edits

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


COMMENTED = (
    "# Team cartridge for example-team.\n"
    "name: example-team\n"
    "policy:\n"
    "  dispatch:\n"
    "    # Cap on lanes in flight.\n"
    "    max_in_flight: 2  # keep small\n"
    "other: kept\n"
)
THREE_HOSTS = ["chair", "host-a", "host-b"]


def _commented(tmp_path, host_names, text=COMMENTED):
    """A profile and a commented team cartridge in `tmp_path`, with `host_names` added to a real hosts table."""
    (tmp_path / "ws").mkdir()
    cartridge_dir = tmp_path / "cartridges" / "example-team"
    cartridge_dir.mkdir(parents=True)
    cartridge = cartridge_dir / "cartridge.yaml"
    cartridge.write_text(text, encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        f"workspace_dir: {tmp_path / 'ws'}\nteam: example-team\ncartridges_dir: {tmp_path / 'cartridges'}\n",
        encoding="utf-8",
    )
    runs_dir = tmp_path / "ws" / "runs"
    runs_dir.mkdir()
    runner = store_cli.runner(runs_dir)
    for name in host_names:
        code, output = runner(host_cmd.add_argv(name, f"{name}.invalid", 4, 1, "", "test"))
        assert code == 0, output
    return profile, cartridge


def test_on_keeps_comments_and_other_keys_through_its_handler(tmp_path):
    profile, cartridge = _commented(tmp_path, THREE_HOSTS)
    assert cartridge.read_text(encoding="utf-8") == COMMENTED
    assert cli._host_orchestrate(_args(profile, "on")) == 0
    text = cartridge.read_text(encoding="utf-8")
    assert _edits(COMMENTED, text) == [("insert", [], ["    local_lanes: decompose"])]
    assert _comments(text) == _comments(COMMENTED)
    data = yaml.safe_load(text)
    assert data["policy"]["dispatch"]["local_lanes"] == "decompose"
    assert data["policy"]["dispatch"]["max_in_flight"] == 2
    assert data["other"] == "kept"
    assert data["name"] == "example-team"


def test_off_keeps_comments_and_other_keys_through_its_handler(tmp_path):
    before = COMMENTED.replace("    max_in_flight: 2", "    local_lanes: decompose\n    max_in_flight: 2")
    profile, cartridge = _commented(tmp_path, THREE_HOSTS, before)
    assert cli._host_orchestrate(_args(profile, "off")) == 0
    text = cartridge.read_text(encoding="utf-8")
    assert _edits(before, text) == [("replace", ["    local_lanes: decompose"], ["    local_lanes: any"])]
    assert _comments(text) == _comments(COMMENTED)
    data = yaml.safe_load(text)
    assert data["policy"]["dispatch"]["local_lanes"] == "any"
    assert data["policy"]["dispatch"]["max_in_flight"] == 2
    assert data["other"] == "kept"
    assert data["name"] == "example-team"
