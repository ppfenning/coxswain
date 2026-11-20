"""harness/extras_tests.py: which tests the extras jobs select, and which skips the guard fails.

The pure core is tested on example inputs; the glue (marks on collected items, the guard's exit status under a real
session, with and without xdist) by a real pytest in a subprocess on a scratch suite. Nothing is mocked.
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from harness import extras_tests

ROOT = Path(__file__).resolve().parents[3]
GRAPHS = ROOT / "src" / "graphs"
CONFTESTS = ("tests/conftest.py", "src/tools/tests/conftest.py", "src/graphs/tests/conftest.py", "src/cartridges/tests/conftest.py")


def test_a_file_that_reaches_for_an_extra_is_marked_extras():
    assert extras_tests.marks_for('pa = pytest.importorskip("pyarrow")') == {"extras"}
    assert extras_tests.marks_for('has_lake = importlib.util.find_spec("pyiceberg") is not None') == {"extras"}


def test_a_file_that_reads_a_test_database_is_marked_postgres_too():
    assert extras_tests.marks_for('os.environ.get("COX_TEST_POSTGRES_URL")') == {"extras", "postgres"}
    assert extras_tests.marks_for('base = os.environ.get("COXSWAIN_TEST_PG_URL")') == {"extras", "postgres"}


def test_a_file_with_neither_is_not_marked():
    assert extras_tests.marks_for("def test_add():\n    assert 1 + 1 == 2\n") == frozenset()


def test_a_skipif_reason_earns_the_marks_of_what_lifts_it():
    assert extras_tests.reason_marks("the optional extra `lake` is not installed") == {"extras"}
    assert extras_tests.reason_marks("COX_TEST_POSTGRES_URL is not set") == {"extras", "postgres"}
    assert extras_tests.reason_marks("rsync is not installed") == frozenset()
    assert extras_tests.reason_marks("pyarrow not installed") == frozenset()  # its own words: the guard catches it


def test_a_backend_parametrized_fixture_marks_only_its_database_param():
    pg = frozenset({"extras", "postgres"})
    assert extras_tests.fixture_param_marks(pg, "postgres") == pg
    assert extras_tests.fixture_param_marks(pg, "sqlite") == frozenset()
    assert extras_tests.fixture_param_marks(pg) == pg  # not parametrized: every test that uses it
    assert extras_tests.fixture_param_marks(frozenset({"extras"}), "sqlite") == {"extras"}


@pytest.mark.parametrize(
    ("reason", "name"),
    [
        ("rsync and git are required", "rsync"),
        ("needs git", "git"),
        ("no git on PATH", "git"),
        ("needs util-linux setsid to escape the group", "setsid"),
        ("root can write to any directory", "root"),
        ("running as a user that ignores file modes", "root"),
        ("load-sensitive timing; set COX_RUN_LOAD_TESTS=1 to run", "load timing"),
        ("POSIX shells and process groups", "win32"),
        ("ripgrep not installed", "ripgrep"),
        ("the trigger statement is sqlite's; the exemption rule itself is dialect-free", "sqlite dialect"),
        ("pyarrow not installed", None),
        ("could not import 'pyarrow': No module named 'pyarrow'", None),
        ("graphs is not checked out beside tools", None),
        # the review's probes: an allowed tool named beside a missing extra is not excused
        ("needs gitpython; pyarrow missing", None),
        ("rsync and pyarrow are required", None),
        ("needs git and pyiceberg", None),
    ],
)
def test_the_allowed_skips_name_what_no_extra_lifts(reason, name):
    assert extras_tests.allowed_skip(reason) == name


def test_every_allowed_skip_says_why():
    assert all(name and reasons and len(why) > 20 for name, reasons, why in extras_tests.ALLOWED_SKIPS)


def test_every_allowed_wording_is_one_a_suite_skips_with():
    """An allowed reason names a skip that exists: each wording appears in a suite's test sources."""
    sources = "\n".join(path.read_text(encoding="utf-8") for path in ROOT.glob("**/tests/**/*.py") if ".venv" not in path.parts)
    missing = [reason for _, reasons, _ in extras_tests.ALLOWED_SKIPS for reason in reasons if reason not in sources]
    assert not missing, missing


def test_unit_excuses_a_marked_or_allowed_skip_and_extras_only_an_allowed_one():
    skips = [
        ("tests/test_a.py::test_one", "could not import 'pyarrow': No module named 'pyarrow'", frozenset({"extras"})),
        ("tests/test_b.py::test_two", "pyarrow not installed", frozenset()),
        ("tests/test_c.py::test_three", "rsync is not installed", frozenset()),
    ]
    assert extras_tests.unexcused(skips, "unit") == ["tests/test_b.py::test_two: pyarrow not installed"]
    probes = [("tests/test_d.py::test_four", "needs gitpython; pyarrow missing", frozenset()),
              ("tests/test_e.py::test_five", "rsync and pyarrow are required", frozenset())]
    assert extras_tests.unexcused(probes, "unit") == [f"{nodeid}: {reason}" for nodeid, reason, _ in probes]
    assert extras_tests.unexcused(skips, "extras") == [
        "tests/test_a.py::test_one: could not import 'pyarrow': No module named 'pyarrow'",
        "tests/test_b.py::test_two: pyarrow not installed",
    ]


def test_every_suite_conftest_registers_the_plugin():
    """The marks and the guard reach a suite only through its conftest's pytest_configure."""
    for relative in CONFTESTS:
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        (configure,) = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "pytest_configure"]
        calls = [ast.unparse(node.func) for node in ast.walk(configure) if isinstance(node, ast.Call)]
        assert "extras_tests.register" in calls, relative


# --- the glue, through a real pytest session -----------------------------------------------------------------------

_CONFTEST = """
import sys

sys.modules["pyarrow"] = None  # the unit jobs' environment: pyarrow is not installed, so importing it fails

from harness import extras_tests


def pytest_configure(config):
    extras_tests.register(config)
"""
_MARKED = """
import pytest

pa = pytest.importorskip("pyarrow")


def test_reads_parquet():
    assert pa
"""
# The probe the review named: a skip in its own words, through no mechanism the scan reads.
_PROBE = """
import pytest

try:
    import pyarrow  # noqa: F401
    HAS = True
except ImportError:
    HAS = False


@pytest.mark.skipif(not HAS, reason="pyarrow not installed")
def test_reads_parquet():
    pass
"""
_ALLOWED = """
import pytest


@pytest.mark.skipif(True, reason="rsync is not installed")
def test_copies():
    pass


def test_plain():
    pass
"""


def _suite(tmp_path: Path, **files: str) -> Path:
    (tmp_path / "conftest.py").write_text(_CONFTEST)
    for name, text in files.items():
        (tmp_path / f"{name}.py").write_text(text)
    return tmp_path


def _pytest(suite: Path, *args: str, guard: str | None) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key not in (extras_tests.GUARD_ENV, "PYTEST_ADDOPTS")}
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(GRAPHS), env.get("PYTHONPATH", "")) if p)
    if guard is not None:
        env[extras_tests.GUARD_ENV] = guard
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(suite), *args]
    return subprocess.run(argv, cwd=suite, env=env, capture_output=True, text=True, timeout=120)


def test_the_marks_land_on_the_collected_tests(tmp_path):
    suite = _suite(tmp_path, test_marked=_MARKED.replace('pa = pytest.importorskip("pyarrow")', "pa = 1  # importorskip( named in a comment"), test_allowed=_ALLOWED)
    proc = _pytest(suite, "--collect-only", "-m", "extras", guard=None)
    assert proc.returncode == 0, proc.stdout
    assert "test_marked.py::test_reads_parquet" in proc.stdout and "test_allowed.py" not in proc.stdout


@pytest.mark.parametrize("workers", [[], ["-n", "2"]], ids=["serial", "xdist"])
def test_the_unit_guard_fails_the_probe_and_passes_marked_and_allowed_skips(tmp_path, workers):
    suite = _suite(tmp_path, test_marked=_MARKED, test_allowed=_ALLOWED, test_probe=_PROBE)
    proc = _pytest(suite, *workers, guard="unit")
    assert proc.returncode == extras_tests.GUARD_FAILED, proc.stdout
    assert "extras guard (unit): 3 skipped, 1 not excused" in proc.stdout
    assert "test_probe.py::test_reads_parquet: pyarrow not installed" in proc.stdout
    # without the probe, the same session passes
    (suite / "test_probe.py").unlink()
    proc = _pytest(suite, *workers, guard="unit")
    assert proc.returncode == 0, proc.stdout
    assert "extras guard (unit): 2 skipped, 0 not excused" in proc.stdout


def test_the_extras_guard_fails_a_selected_test_that_still_skips(tmp_path):
    suite = _suite(tmp_path, test_marked=_MARKED, test_allowed=_ALLOWED)
    proc = _pytest(suite, guard="extras")
    assert proc.returncode == extras_tests.GUARD_FAILED, proc.stdout
    assert "extras guard (extras): 2 skipped, 1 not excused" in proc.stdout and "test_marked.py" in proc.stdout


def test_a_guard_mode_it_does_not_know_fails_the_session(tmp_path):
    proc = _pytest(_suite(tmp_path, test_allowed=_ALLOWED), guard="1")
    assert proc.returncode == extras_tests.GUARD_FAILED and "must be one of unit, extras" in proc.stdout


def test_without_the_guard_a_skip_fails_nothing(tmp_path):
    proc = _pytest(_suite(tmp_path, test_probe=_PROBE), guard=None)
    assert proc.returncode == 0 and "extras guard" not in proc.stdout
