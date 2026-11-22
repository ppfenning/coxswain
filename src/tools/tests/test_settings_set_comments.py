from __future__ import annotations

import argparse

import yaml

from agent_tools import cli
from tests.comment_helpers import _comments, _edits

CARTRIDGE = (
    "# Team cartridge for example-team.\n"
    "name: example-team\n"
    "policy:\n"
    "  dispatch:\n"
    "    # Cap on lanes in flight.\n"
    "    max_in_flight: 2  # keep small\n"
    "    local_lanes: any\n"
    "  budget:\n"
    "    daily: 5\n"
    "other: kept\n"
    "tags:\n"
    "  - a\n"
    "  - b\n"
)


def _setup(tmp_path, text=CARTRIDGE):
    """A profile naming a temp workspace and a temp team cartridge holding `text`."""
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
    return profile, cartridge


def _args(profile, key, value):
    return argparse.Namespace(
        profile=str(profile), scope="cartridge", key=key, value=value, dry_run=False, commit=False,
    )


def test_set_changes_one_line_and_keeps_comments_and_other_keys(tmp_path, capsys):
    profile, cartridge = _setup(tmp_path)
    assert cli._settings_set(_args(profile, "policy.dispatch.local_lanes", "decompose")) == 0
    capsys.readouterr()
    text = cartridge.read_text(encoding="utf-8")
    assert _edits(CARTRIDGE, text) == [("replace", ["    local_lanes: any"], ["    local_lanes: decompose"])]
    assert _comments(text) == _comments(CARTRIDGE)
    expected = yaml.safe_load(CARTRIDGE)
    expected["policy"]["dispatch"]["local_lanes"] = "decompose"
    assert yaml.safe_load(text) == expected


def test_set_of_an_absent_key_keeps_every_original_line(tmp_path, capsys):
    before = CARTRIDGE.replace("    local_lanes: any\n", "")
    profile, cartridge = _setup(tmp_path, before)
    assert cli._settings_set(_args(profile, "policy.dispatch.local_lanes", "decompose")) == 0
    capsys.readouterr()
    text = cartridge.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert [line for line in lines if line in before.splitlines()] == before.splitlines()
    assert "    local_lanes: decompose" in lines
    expected = yaml.safe_load(before)
    expected["policy"]["dispatch"]["local_lanes"] = "decompose"
    assert yaml.safe_load(text) == expected
