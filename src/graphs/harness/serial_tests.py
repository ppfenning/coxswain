"""pytest hooks that keep `@pytest.mark.serial` tests out of the xdist workers and run them alone afterwards.

A timing-sensitive test (a startup-time budget, a lease renewal, a kill-after-timeout) fails when `-n auto`
workers load the machine, and passes when run alone. `xdist_group` would not help: it puts a group on one
worker, but the other workers still run beside it. So the workers deselect `serial` tests, and the
controller runs them in one plain `pytest -p no:xdist -m serial` once the workers are done.

A suite's `tests/conftest.py` imports `pytest_collection_modifyitems` and `pytest_sessionfinish` from here
and registers the `serial` marker in its `pytest.ini`. With `-n 0` or no xdist the tests run in place.
"""

from __future__ import annotations

import subprocess
import sys

NO_TESTS_COLLECTED = 5  # pytest's exit code when nothing was selected


def _is_worker(config) -> bool:
    return hasattr(config, "workerinput")


def pytest_collection_modifyitems(config, items) -> None:
    if not _is_worker(config):
        return
    serial = [item for item in items if item.get_closest_marker("serial") is not None]
    if serial:
        config.hook.pytest_deselected(items=serial)
        items[:] = [item for item in items if item not in serial]


def _serial_command(config) -> list[str]:
    keyword = ["-k", config.option.keyword] if config.option.keyword else []
    return [sys.executable, "-m", "pytest", "-p", "no:xdist", "-p", "no:cacheprovider", "-q", "-m", "serial", *keyword, *config.args]


def pytest_sessionfinish(session, exitstatus) -> None:
    config = session.config
    if _is_worker(config) or not getattr(config.option, "numprocesses", None) or config.option.collectonly:
        return
    proc = subprocess.run(_serial_command(config), cwd=config.invocation_params.dir, capture_output=True, text=True)
    if proc.returncode == NO_TESTS_COLLECTED:
        return
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        lines = (proc.stdout + proc.stderr).strip().splitlines()
        reporter.write_line("serial tests, run apart from the xdist workers: " + (lines[-1] if lines else f"exit {proc.returncode}"))
        if proc.returncode != 0:
            reporter.write_line("\n".join(lines[-40:]))
    if proc.returncode != 0:
        session.exitstatus = proc.returncode
    elif session.exitstatus == NO_TESTS_COLLECTED:
        session.exitstatus = 0
