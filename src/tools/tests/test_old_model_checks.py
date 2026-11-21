"""L2 leaves the old model as it was: this repository's marked `.agent-checks` gives the chair's carry, `cox runs land`
and the lander's gates the commands main gave them (origin/main at #1299, before levels). No mocks: real files, the
real parsers, and the lander's gate run in a real directory."""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from agent_tools import cli_land, lander_ports
from agent_tools.chair_exec_edge import carry_commands

ROOT = Path(__file__).resolve().parents[3]
# Main's `.agent-checks`, its command lines in order: what the carry, a land and every old-model task ran.
MAIN_COMMANDS = [
    "cd src/tools && pytest -q -n auto", "cd src/graphs && pytest -q -n auto", "cd src/cartridges && pytest -q -n auto",
    "pytest -q -rs smoke", "python .github/check_clean.py . --require-names", "python .github/check_unwired.py",
    "python .github/check_types.py",
]
MAIN_TEXT = "# a comment\n" + "\n".join(MAIN_COMMANDS) + "\n\n# another\n"


def _real() -> str:
    return (ROOT / ".agent-checks").read_text(encoding="utf-8")


def test_the_carry_runs_the_same_seven_commands_as_main_each_once():
    assert carry_commands(MAIN_TEXT) == MAIN_COMMANDS  # an unmarked file: every command line, in order, as before
    carried = carry_commands(_real())
    assert sorted(carried) == sorted(MAIN_COMMANDS) and len(carried) == len(MAIN_COMMANDS), carried
    assert 'pytest -q -rs smoke -m "not forge"' not in carried  # the phase section is the phase lane's alone


def test_a_land_checks_what_main_checked_and_its_ticket_gate_is_the_cartridges_alone(tmp_path, monkeypatch):
    shutil.copy(ROOT / ".agent-checks", tmp_path / ".agent-checks")
    overlay = yaml.safe_load((ROOT / ".agent" / "cartridge.yaml").read_text(encoding="utf-8"))["landing_areas"]
    monkeypatch.setattr(cli_land, "_cartridge_landing", lambda repo, profile: (dict(overlay), None))
    monkeypatch.setattr(cli_land, "_required_checks", lambda *args: ("test",))  # the forge read `land_checks: ci` does
    facts = cli_land._land_repo_facts(tmp_path, {}, "main")
    cmds = [c["cmd"] for c in facts["checks"]]
    assert sorted(cmds) == sorted(MAIN_COMMANDS) and len(cmds) == len(MAIN_COMMANDS), cmds
    assert facts["ticket_checks"] == []


def test_the_landers_level_and_ticket_gates_ignore_the_sections_of_a_tree_on_the_old_model(tmp_path):
    """An old-model landing record (one that names no `lane_model`) keeps main's gates: the cartridge's lists alone.
    A section that would fail if it ran shows it does not. The new model's gate is `lander.PHASE_GATE`."""
    (tmp_path / ".agent-checks").write_text(_real() + "\n# level: phase\nexit 7\n# level: initiative\nexit 8\n")
    overlay = yaml.safe_load((ROOT / ".agent" / "cartridge.yaml").read_text(encoding="utf-8"))["landing_areas"]
    for gate in ("level", "ticket"):
        assert lander_ports.run_gate(tmp_path, tmp_path, gate, overlay, None) == (True, "")
