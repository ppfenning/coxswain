"""H2's type check: the lane wrapper's skip rule on example inputs, then pyright itself on the project's own config."""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "check_types.py"
_SPEC = importlib.util.spec_from_file_location("check_types", SCRIPT)
check_types = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_types)


def test_nothing_missing_runs_the_check():
    assert check_types.unavailable([], required=False) is None
    assert check_types.unavailable([], required=True) is None


def test_a_lane_without_pyright_skips_and_says_how_to_install_it():
    code, line = check_types.unavailable(["pyright", "duckdb"], required=False)
    assert code == 0
    assert line.startswith("types: skipped: pyright, duckdb not installed for ")
    assert line.endswith("run `uv sync --frozen --all-extras --group dev`")


def test_a_required_run_without_pyright_fails():
    code, line = check_types.unavailable(["pyright"], required=True)
    assert code == 1
    assert line.startswith("types: failed: pyright not installed")


@pytest.mark.parametrize(("value", "required"), [(None, False), ("", False), ("0", False), ("no", False), ("1", True), ("yes", True)])
def test_the_required_flag_reads_like_smokes(value, required):
    assert check_types.is_required(value) is required


# A parameter with no annotation: pyright's strict level reports it (reportMissingParameterType), its basic level does not.
STRICT_ONLY = "def probe(value):\n    return value\n"


def _pyright(project: Path, relative: str) -> subprocess.CompletedProcess[str]:
    target = project / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(STRICT_ONLY)
    argv = [sys.executable, "-m", "pyright", "--pythonpath", sys.executable, "--project", str(project), str(target)]
    return subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False)


@pytest.mark.skipif(importlib.util.find_spec("pyright") is None, reason="pyright is not installed (the dev group carries it)")
def test_the_store_glob_is_checked_strictly_and_a_basic_module_is_not(tmp_path):
    """The project's own [tool.pyright], copied beside a probe module: under `harness/store*.py` the strict-only
    finding fails the check; the same text in a basic-level module passes."""
    shutil.copy(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    strict = _pyright(tmp_path, "src/graphs/harness/store_probe.py")
    assert strict.returncode == 1, strict.stdout
    assert "reportMissingParameterType" in strict.stdout
    basic = _pyright(tmp_path, "src/graphs/harness/probe.py")
    assert basic.returncode == 0, basic.stdout
