"""`smoke/required.py` in a real pytest: under `COX_SMOKE_REQUIRED` a skipped module or test fails the run.

Each case runs pytest in a folder of its own with only that plugin loaded (`-p smoke.required`), so what it proves is
the hook, not this suite's fixtures. Without the flag the same files skip and the run passes.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from smoke import plan

ROOT = Path(__file__).resolve().parents[1]
MISSING_MODULE = "cox_smoke_no_such_module"
ALLOWED = next(iter(plan.ALLOWED_SKIPS))


def _pytest(folder: Path, files: dict[str, str], *, required: bool) -> subprocess.CompletedProcess[str]:
    for name, text in files.items():
        (folder / name).write_text(text, encoding="utf-8")
    (folder / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if key not in {plan.REQUIRED_ENV, plan.SHARD_ENV}}
    env["PYTHONPATH"] = str(ROOT)
    if required:
        env[plan.REQUIRED_ENV] = "1"
    argv = [sys.executable, "-m", "pytest", "-q", "-rfEs", "-p", "smoke.required", "-p", "no:cacheprovider",
            "-p", "no:xdist", "-c", str(folder / "pytest.ini"), "--rootdir", str(folder), str(folder)]
    return subprocess.run(argv, cwd=folder, env=env, capture_output=True, text=True, timeout=120)


IMPORT_SKIP = {
    "test_needs_missing.py": f'import pytest\n\npytest.importorskip("{MISSING_MODULE}")\n\n\ndef test_never_runs():\n    pass\n',
    "test_other.py": "def test_runs():\n    pass\n",
}
SKIPS = f'''import pytest


def test_skips_in_the_body():
    pytest.skip("no tool on PATH")


@pytest.mark.skipif(True, reason={ALLOWED!r})
def test_platform_skip():
    pass


@pytest.mark.xfail(strict=True, reason="known drift")
def test_known_drift():
    assert False


def test_runs():
    pass
'''


def test_a_module_skipped_at_import_fails_the_run_under_the_flag(tmp_path):
    done = _pytest(tmp_path, IMPORT_SKIP, required=True)
    out = done.stdout + done.stderr
    assert done.returncode != 0, out
    assert f"skipped under {plan.REQUIRED_ENV}" in out and MISSING_MODULE in out, out
    assert "1 error" in out, out


def test_without_the_flag_the_same_module_skips_and_the_run_passes(tmp_path):
    done = _pytest(tmp_path, IMPORT_SKIP, required=False)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "1 passed, 1 skipped" in done.stdout, done.stdout


def test_under_the_flag_a_test_skip_fails_and_an_allowed_skip_and_an_xfail_stand(tmp_path):
    done = _pytest(tmp_path, {"test_skips.py": SKIPS}, required=True)
    out = done.stdout + done.stderr
    assert done.returncode == 1, out
    assert "1 failed, 1 passed, 1 skipped, 1 xfailed" in out, out
    assert "FAILED test_skips.py::test_skips_in_the_body" in out, out
    assert f"skipped under {plan.REQUIRED_ENV}" in out and "no tool on PATH" in out, out
