"""The repo's own overlay leaves the full suite to CI: task checks run narrowed, and no local level or ticket gate repeats the suite."""

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
def test_no_local_gate_repeats_the_full_suite(key):
    # Pat, 2026-11-10: test only what a task touches, and leave the full suite to CI on the PR.
    assert key not in overlay()["landing_areas"]


def test_agent_checks_keeps_task_checks():
    lines = [
        line
        for line in (ROOT / ".agent-checks").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    pytest_lines = [f"cd src/{name} && pytest -q -n auto" for name in ("tools", "graphs", "cartridges")]
    assert lines == pytest_lines + [CLEAN_LINE]
