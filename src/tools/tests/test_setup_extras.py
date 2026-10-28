import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_tools import cli, extras_lock, store_dialect, store_probe
from agent_tools.extras_lock import LockError, Pin

H1, H2, H3 = ("sha256:" + c * 64 for c in "abc")
LOCK = f"""
version = 1

[[package]]
name = "coxswain"
version = "1"
source = {{ editable = "." }}

[package.optional-dependencies]
postgres = [{{ name = "psycopg", extra = ["binary"] }}]
parquet = [{{ name = "pyarrow" }}]
lake = [{{ name = "zstandard" }}]
traces = [{{ name = "pyarrow" }}, {{ name = "zstandard" }}]

[[package]]
name = "psycopg"
version = "3.3"
dependencies = [{{ name = "tzdata", marker = "sys_platform == 'win32'" }}]
wheels = [{{ url = "u", hash = "{H1}" }}]

[package.optional-dependencies]
binary = [{{ name = "psycopg-binary", marker = "implementation_name != 'pypy'" }}]

[[package]]
name = "psycopg-binary"
version = "3.3"
wheels = [{{ url = "u", hash = "{H2}" }}, {{ url = "u", hash = "{H3}" }}]

[[package]]
name = "tzdata"
version = "2026.5"
wheels = [{{ url = "u", hash = "{H1}" }}]

[[package]]
name = "pyarrow"
version = "26"
wheels = [{{ url = "u", hash = "{H2}" }}]

[[package]]
name = "zstandard"
version = "0.25"
wheels = [{{ url = "u", hash = "{H3}" }}]
"""
ENV = {"sys_platform": "linux", "implementation_name": "cpython", "python_full_version": "3.14.8"}
PREFIX = "/opt/cox/libexec"
ROOT = Path(__file__).resolve().parents[3]


def _pins() -> tuple[Pin, ...]:
    return extras_lock.parse_requirements(extras_lock.render_requirements(LOCK))


def test_render_requirements_pins_every_package_with_hashes_and_the_extras_that_need_it():
    lines = extras_lock.render_requirements(LOCK).splitlines()[2:]
    assert lines == [
        f"psycopg==3.3 --hash={H1}  # extras: postgres",
        f"psycopg-binary==3.3 ; implementation_name != 'pypy' --hash={H2} --hash={H3}  # extras: postgres",
        f"pyarrow==26 --hash={H2}  # extras: traces,parquet",
        f"tzdata==2026.5 ; sys_platform == 'win32' --hash={H1}  # extras: postgres",
        f"zstandard==0.25 --hash={H3}  # extras: traces,lake",
    ]


def test_a_package_without_a_wheel_is_an_error_value_naming_it():
    lock = LOCK.replace(f'name = "zstandard"\nversion = "0.25"\nwheels = [{{ url = "u", hash = "{H3}" }}]', 'name = "zstandard"\nversion = "0.25"')
    result = extras_lock.render_requirements(lock)
    assert isinstance(result, LockError) and "zstandard" in result.reason


def test_an_unknown_extra_is_an_error_value():
    assert extras_lock.lock_pins(LOCK, ("nope",)) == LockError("uv.lock has no extra named nope")


def test_write_requirements_writes_nothing_when_the_lock_cannot_be_rendered(tmp_path):
    (tmp_path / "uv.lock").write_text("not = [toml")
    assert extras_lock.write_requirements(tmp_path / "uv.lock", tmp_path / "out.txt") == 1
    assert not (tmp_path / "out.txt").exists()


def test_the_shipped_file_is_what_uv_lock_renders_and_every_line_is_hashed():
    lock = ROOT / "uv.lock"
    if not lock.exists():
        pytest.skip("not a checkout: no uv.lock beside the package")
    shipped = extras_lock.shipped_requirements()
    assert shipped == extras_lock.render_requirements(lock.read_text()), "run: python -m agent_tools.extras_lock"
    body = [ln for ln in shipped.splitlines() if ln and not ln.startswith("#")]
    assert body and all(re.search(r"--hash=sha256:[0-9a-f]{64}", ln) for ln in body)
    assert {"psycopg", "psycopg-binary", "pyarrow", "duckdb", "pyiceberg-core", "zstandard"} <= {p.name for p in extras_lock.parse_requirements(shipped)}


def test_parse_requirements_refuses_an_unhashed_line():
    assert isinstance(extras_lock.parse_requirements("pyarrow==26\n"), LockError)


def test_marker_applies_reads_simple_clauses_and_defers_what_it_cannot_read():
    assert extras_lock.marker_applies("implementation_name != 'pypy'", ENV)
    assert not extras_lock.marker_applies("sys_platform == 'win32'", ENV)
    assert extras_lock.marker_applies("python_full_version < '3.15'", ENV)
    assert not extras_lock.marker_applies("python_full_version < '3.9' or sys_platform == 'win32'", ENV)
    assert extras_lock.marker_applies("extra == 'x'", ENV)


def test_plan_install_builds_the_pip_argv_for_the_running_prefix():
    plan = extras_lock.plan_install(_pins(), ("postgres",), PREFIX, "req.txt", {}, ENV)
    assert plan.action == "install"
    assert plan.argv == (
        f"{PREFIX}/bin/python", "-m", "pip", "install", "--require-hashes", "--only-binary", ":all:",
        "--no-deps", "--disable-pip-version-check", "-r", "req.txt",
    )
    assert [p.name for p in plan.pins] == ["psycopg", "psycopg-binary", "tzdata"]


def test_plan_install_is_present_when_every_applicable_pin_is_at_its_locked_version():
    installed = {"psycopg": "3.3", "psycopg-binary": "3.3"}
    assert extras_lock.plan_install(_pins(), ("postgres",), PREFIX, "r", installed, ENV).action == "present"


def test_plan_install_reinstalls_when_one_pin_is_a_version_behind():
    installed = {"psycopg": "3.3", "psycopg-binary": "3.2"}
    assert extras_lock.plan_install(_pins(), ("postgres",), PREFIX, "r", installed, ENV).action == "install"


class _Runner:
    def __init__(self, code=0):
        self.calls, self.code, self.seen = [], code, []

    def __call__(self, argv):
        self.calls.append(argv)
        self.seen.append(Path(argv[-1]).read_text())
        return SimpleNamespace(returncode=self.code)


def _run(extras, dry_run, runner, installed=None):
    out: list[str] = []
    code = extras_lock.run_extras(
        extras, dry_run, prefix=PREFIX, requirements_text=extras_lock.render_requirements(LOCK), runner=runner,
        installed=lambda _pins: installed or {}, env=ENV, out=out.append,
    )
    return code, "\n".join(out)


def test_dry_run_prints_the_plan_calls_no_runner_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = _Runner()
    code, text = _run(("postgres",), True, runner)
    assert code == 0 and runner.calls == []
    assert "would install extras postgres" in text and "psycopg-binary==3.3" in text and "--require-hashes" in text
    assert list(tmp_path.iterdir()) == []


def test_already_present_says_so_and_calls_no_runner():
    runner = _Runner()
    code, text = _run(("postgres",), False, runner, {"psycopg": "3.3", "psycopg-binary": "3.3"})
    assert code == 0 and runner.calls == [] and "already installed at the locked versions" in text


def test_install_runs_pip_on_a_file_holding_only_the_selected_pins_and_returns_its_exit_code():
    runner = _Runner(code=7)
    code, _ = _run(("traces",), False, runner)
    assert code == 7 and runner.calls[0][:5] == [f"{PREFIX}/bin/python", "-m", "pip", "install", "--require-hashes"]
    assert [ln.split("==")[0] for ln in runner.seen[0].splitlines() if not ln.startswith("#")] == ["pyarrow", "zstandard"]


def test_an_unknown_extra_is_refused_with_exit_2():
    runner = _Runner()
    code, text = _run(("analytics",), False, runner)
    assert code == 2 and runner.calls == [] and "choose from traces, parquet, lake, postgres" in text


def test_the_command_is_registered_with_a_dry_run_and_with_flags():
    a = cli.build_parser().parse_args(["setup", "extras", "--with", "postgres", "lake", "--dry-run"])
    assert a.with_extras == ["postgres", "lake"] and a.dry_run is True
    assert cli.build_parser().parse_args(["setup", "extras"]).with_extras is None


def test_the_missing_client_rows_name_the_command():
    assert "cox setup extras" in store_dialect.MISSING_PSYCOPG
    assert cli._store_error(RuntimeError(store_dialect.MISSING_PSYCOPG)) == store_dialect.MISSING_PSYCOPG
    for component in ("postgres", "object_store"):
        sentence = store_probe.classify_failure("ModuleNotFoundError", component)
        assert sentence.startswith(component) and "cox setup extras" in sentence
