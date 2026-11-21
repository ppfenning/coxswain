"""Records each smoke file's seconds into `smoke/timings.json`, the weights CI's shards are split by (`plan.file_weights`).

A pytest plugin, loaded by name. Regenerate the file from the repository root, serially, as CI runs the shards:

    COX_SMOKE_TIMINGS=smoke/timings.json pytest -q -rs -p smoke.timings -m "not forge" smoke

A test's seconds are its setup, call and teardown, less the session fixtures its setup started (the install, the
Postgres server, the store template): those run once per shard whichever file comes first, so they weigh no file. A
run on a few files rewrites only theirs. The arithmetic is `plan.file_seconds` and `plan.timings_text`.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from smoke import plan

_durations: list[tuple[str, float]] = []
_deselected: list[str] = []
_session_s = [0.0]  # session fixture seconds set up since the last test's setup report


def pytest_configure(config: pytest.Config) -> None:
    if hasattr(config, "workerinput") or getattr(config.option, "numprocesses", None):
        raise pytest.UsageError("smoke.timings measures a serial run, as CI's shards are: drop -n")
    if os.environ.get(plan.SHARD_ENV):  # a shard deselects the other shards' files, which would then weigh 0
        raise pytest.UsageError(f"smoke.timings measures every file: unset {plan.SHARD_ENV}")


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef: pytest.FixtureDef, request: pytest.FixtureRequest) -> Iterator[None]:
    started = time.monotonic()
    try:
        return (yield)
    finally:
        if fixturedef.scope == "session":
            _session_s[0] += time.monotonic() - started


def pytest_deselected(items: list[pytest.Item]) -> None:
    _deselected.extend(item.nodeid for item in items)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    session = 0.0
    if report.when == "setup":
        session, _session_s[0] = _session_s[0], 0.0
    _durations.append((report.nodeid, report.duration - session))


def pytest_sessionfinish(session: pytest.Session) -> None:
    target = os.environ.get(plan.TIMINGS_ENV)
    if not target:
        return
    path = Path(target)
    old = plan.read_timings(path.read_text(encoding="utf-8")) if path.exists() else {}
    path.write_text(plan.timings_text(old, plan.file_seconds(_durations, _deselected)), encoding="utf-8")
