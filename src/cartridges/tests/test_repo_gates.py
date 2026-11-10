"""The repo's own overlay runs each component's full suite at the level and ticket gate."""

import re
from pathlib import Path

import pytest
import yaml

from core.cartridge import overlay_errors

ROOT = Path(__file__).resolve().parents[3]
COMPONENTS = ["cartridges", "graphs", "tools"]
CLEAN_LINE = "python .github/check_clean.py . --require-names"


def overlay() -> dict:
    return yaml.safe_load((ROOT / ".agent" / "cartridge.yaml").read_text())


def test_overlay_is_accepted_and_ci_lands():
    loaded = overlay()
    assert overlay_errors(loaded) == []
    assert loaded["landing_areas"]["land_checks"] == "ci"


@pytest.mark.parametrize("key", ["level_checks", "ticket_checks"])
def test_gate_list_names_each_component_once(key):
    entries = overlay()["landing_areas"][key]
    dirs = [re.match(r"cd src/(\w+) && ", entry["cmd"]).group(1) for entry in entries]
    assert sorted(dirs) == COMPONENTS
    assert {entry["timeout"] for entry in entries} == {1500}


def test_agent_checks_keeps_task_checks():
    lines = [
        line
        for line in (ROOT / ".agent-checks").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    pytest_lines = [f"cd src/{name} && pytest -q -n auto" for name in ("tools", "graphs", "cartridges")]
    assert lines == pytest_lines + [CLEAN_LINE]
