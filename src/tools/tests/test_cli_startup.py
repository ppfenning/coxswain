"""Guard lazy command imports: the heavy modules stay unloaded and startup stays inside a ceiling."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.startup_timing import STARTUP_MULTIPLE, check_rounds, time_rounds

REPO_ROOT = Path(__file__).resolve().parent.parent

HEAVY_MODULES = ("psycopg", "duckdb", "pyarrow")

# Local targets for the median wall time of one `cox` process, in seconds.
# Measured on the finished code (two sessions, 7 timed runs each, macOS, warm cache):
#   cox --help                  median 49 ms  (about 72 ms before the command modules went lazy)
#   cox route chair status      median 55 ms
# These are never asserted. They are passed to check_rounds so a failure message prints them.
# The pass condition is the median, over interleaved rounds, of cox's time over a `python -c pass` spawned in the
# same round (STARTUP_MULTIPLE; tests/startup_timing.py says why the rounds interleave).
HELP_TARGET_S = 0.25
CHAIR_STATUS_TARGET_S = 0.30

TIMED_RUNS = 7

_ISOLATION_SNIPPET = (
    f"import json, sys, agent_tools.cli\nprint(json.dumps([m for m in {HEAVY_MODULES!r} if m in sys.modules]))\n"
)


def _env() -> dict[str, str]:
    """The parent environment with the repo root first on PYTHONPATH, so the subprocess imports this tree."""
    inherited = os.environ.get("PYTHONPATH", "")
    return {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in (str(REPO_ROOT), inherited) if p)}


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, cwd=REPO_ROOT, env=_env(), check=False)


def _cox(*args: str) -> list[str]:
    return [sys.executable, "-m", "agent_tools.cli", *args]


def test_cli_import_does_not_load_heavy_modules() -> None:
    proc = _run([sys.executable, "-c", _ISOLATION_SNIPPET])
    assert proc.returncode == 0, proc.stderr
    loaded = json.loads(proc.stdout)
    assert loaded == [], f"importing agent_tools.cli loaded {loaded}"


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = tmp_path / "profile.yaml"
    path.write_text(f"workspace_dir: {workspace}\n", encoding="utf-8")
    return path


def _assert_relative_startup(argv: list[str], absolute_target: float) -> None:
    result = check_rounds(time_rounds(argv, _env(), TIMED_RUNS, REPO_ROOT), STARTUP_MULTIPLE, absolute_target)
    assert result.ok, result.message


def test_help_startup_relative_to_baseline() -> None:
    _assert_relative_startup(_cox("--help"), HELP_TARGET_S)


# Modules `cox route chair status` must not import: together about 14 ms of its startup, which #1248 added by
# importing two names from forge_forgejo into unit_env (chair_service imports unit_env).
CHAIR_STATUS_UNLOADED = ("agent_tools.forge_forgejo", "agent_tools.forge_github", "agent_tools.land", "agent_tools.chair_types")


def test_route_chair_status_does_not_import_the_forge_or_land_modules(profile: Path) -> None:
    proc = _run([sys.executable, "-X", "importtime", "-m", "agent_tools.cli", "route", "chair", "status",
                 "--profile", str(profile)])
    imported = {line.rsplit("|", 1)[1].strip() for line in proc.stderr.splitlines() if line.startswith("import time:")}
    assert "agent_tools.chair_service" in imported, proc.stderr[-2000:]  # the command ran its real import path
    assert sorted(imported.intersection(CHAIR_STATUS_UNLOADED)) == []


# serial: its startup time is compared with a baseline, and `-n auto` contention skews the two runs differently
# (quarantined an approved task on 2026-10-30); it passed alone.
@pytest.mark.serial
def test_route_chair_status_startup_relative_to_baseline(profile: Path) -> None:
    _assert_relative_startup(_cox("route", "chair", "status", "--profile", str(profile)), CHAIR_STATUS_TARGET_S)
