"""pytest hooks that mark the tests the optional extras or a test Postgres un-skip, and fail any other skip.

CI's `unit` jobs install no extras and name no test database, so a test that reaches for pyarrow, pyiceberg or
another extra through `pytest.importorskip` or `importlib.util.find_spec`, or that reads COX_TEST_POSTGRES_URL or
COXSWAIN_TEST_PG_URL, skips there. The `extras` jobs install every extra and start a Postgres, and run only those
tests: `-m extras` instead of the whole suite again.

The marks come from source (`marks_for`): a test file that names one of those mechanisms gets `extras` on every test
in it, and one that names a test database's variable also gets `postgres`, which the extras jobs run serially, since
those tests pg_dump the test database. A test also gets the marks of every fixture it uses, from the fixture's own
source, so a conftest fixture that skips without a database (graphs' `store_url`) marks the tests on its database
param, and the marks a `skipif` reason earns (`reason_marks`), so a marker imported from another module marks the
tests it skips. A test can also carry `@pytest.mark.extras` (or `postgres`) by hand, for a skip none of that sees. A
test marked by mistake only runs twice.

The other direction is the guard, which fails every skip it cannot excuse (`unexcused`). With COX_EXTRAS_GUARD=unit,
as the `unit` jobs set it, a skip is excused when the test is marked `extras`, since the extras jobs run it, or when
its reason is on `ALLOWED_SKIPS`, a short list of skips no extra lifts. With COX_EXTRAS_GUARD=extras, as the extras
jobs set it, only `ALLOWED_SKIPS` excuses a skip: a test the extras jobs select must run there. So a test that skips
by any other route (`try: import pyarrow` and a `skipif` in its own words, say) fails `unit` until it is marked.

A suite's `tests/conftest.py` calls `register(config)` from its `pytest_configure`.
"""

from __future__ import annotations

import inspect
import os
from functools import cache
from pathlib import Path

EXTRAS_SIGNS = ("importorskip(", "find_spec(")
POSTGRES_SIGNS = ("COX_TEST_POSTGRES_URL", "COXSWAIN_TEST_PG_URL")
# How the skips the extras lift read: importorskip's own message, a skip naming an optional extra, a missing database.
_SKIP_PHRASES = ("could not import", "optional extra", *POSTGRES_SIGNS)
MARKERS = (
    "extras: reaches for an optional extra or a test Postgres; the extras jobs run it (harness/extras_tests.py)",
    "postgres: reads a test Postgres; the extras jobs run it serially (harness/extras_tests.py)",
)
GUARD_FAILED = 1
GUARD_ENV = "COX_EXTRAS_GUARD"
GUARD_MODES = ("unit", "extras")
# The skips no extra and no test database lifts, so no job runs the test: (name, the whole reasons, why allowed).
# A reason is excused only when it is one of these exactly, so a reason that also names an extra is not.
ALLOWED_SKIPS = (
    ("rsync", ("rsync is required", "rsync is not installed", "rsync and git are required"),
     "the remote-fetch tests copy with rsync; a host without it cannot run them"),
    ("git", ("needs git", "no git on PATH"), "the test drives a real git; a host without git cannot run it"),
    ("setsid", ("needs util-linux setsid to escape the group", "setsid is Linux-only"),
     "the test leaves its process group with util-linux setsid, which is Linux only"),
    ("root", ("root can write to any directory", "root reads files regardless of mode",
              "running as a user that ignores file modes", "root reads a mode-000 directory"),
     "a file-mode refusal does not hold for root"),
    ("win32", ("needs /bin/sh", "POSIX shells and process groups"), "POSIX-only behaviour, skipped on Windows"),
    ("ripgrep", ("ripgrep not installed",), "the digest test shells out to rg, which the CI image does not carry"),
    ("sqlite dialect", ("the trigger statement is sqlite's; the exemption rule itself is dialect-free",),
     "a sqlite-only statement, skipped on a dialect test's postgres param"),
)


def marks_for(source: str) -> frozenset[str]:
    """The marks a test file's source earns: `postgres` implies `extras`."""
    postgres = any(sign in source for sign in POSTGRES_SIGNS)
    extras = postgres or any(sign in source for sign in EXTRAS_SIGNS)
    return frozenset({"extras"} if extras else set()) | frozenset({"postgres"} if postgres else set())


def reason_marks(reason: str) -> frozenset[str]:
    """The marks a skip's reason earns: `extras` for any skip an extra or a database lifts, `postgres` for a database."""
    if not is_extras_skip(reason):
        return frozenset()
    return frozenset({"extras", "postgres"} if any(sign in reason for sign in POSTGRES_SIGNS) else {"extras"})


def is_extras_skip(reason: str) -> bool:
    """Whether a skip's reason is one an installed extra or a named test database would lift."""
    return any(phrase in reason for phrase in _SKIP_PHRASES)


def allowed_skip(reason: str) -> str | None:
    """The `ALLOWED_SKIPS` name whose wording is the skip's whole reason, or None."""
    return next((name for name, reasons, _ in ALLOWED_SKIPS if reason.strip() in reasons), None)


def unexcused(skips: list[tuple[str, str, frozenset[str]]], mode: str) -> list[str]:
    """`<nodeid>: <reason>` for each (nodeid, reason, marks) skip the guard fails in `mode` (`unit` or `extras`)."""
    return [
        f"{nodeid}: {reason}"
        for nodeid, reason, marks in skips
        if allowed_skip(reason) is None and not (mode == "unit" and "extras" in marks)
    ]


_UNSET = object()


def fixture_param_marks(marks: frozenset[str], param: object = _UNSET) -> frozenset[str]:
    """A fixture's marks as one test earns them. A fixture parametrized by backend (graphs' `store_url`: sqlite and
    postgres) skips only its database param, so a test on another param earns none: it runs, and passes, in `unit`."""
    if param is _UNSET or "postgres" not in marks:
        return marks
    return marks if any(word in str(param).lower() for word in ("postgres", "pg")) else frozenset()


@cache
def _file_marks(path: Path) -> frozenset[str]:
    try:
        return marks_for(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return frozenset()


@cache
def _fixture_marks(func) -> frozenset[str]:
    try:
        return marks_for(inspect.getsource(func))
    except (OSError, TypeError):
        return frozenset()


def _item_marks(item) -> frozenset[str]:
    """The file's marks, those of every fixture the test uses, and those its `skipif` reasons earn."""
    marks = _file_marks(Path(item.path))
    for skipif in item.iter_markers("skipif"):
        marks |= reason_marks(str(skipif.kwargs.get("reason", "")))
    fixtures = getattr(item, "_fixtureinfo", None)
    params = getattr(getattr(item, "callspec", None), "params", {})
    for name, definitions in (fixtures.name2fixturedefs.items() if fixtures is not None else ()):
        for definition in definitions:
            marks |= fixture_param_marks(_fixture_marks(definition.func), params.get(name, _UNSET))
    return marks


def _reason(report) -> str:
    longrepr = report.longrepr
    reason = str(longrepr[2]) if isinstance(longrepr, tuple) and len(longrepr) == 3 else str(longrepr)
    return reason.removeprefix("Skipped: ")


class _Plugin:
    def __init__(self, config) -> None:
        self.config = config
        self.skips: list[tuple[str, str, frozenset[str]]] = []
        self.controller = not hasattr(config, "workerinput")

    def pytest_itemcollected(self, item) -> None:
        for name in sorted(_item_marks(item)):
            item.add_marker(name)

    def _record(self, report) -> None:
        if not (self.controller and report.skipped) or hasattr(report, "wasxfail"):  # an xfail reports as skipped
            return
        nodeid = report.nodeid
        # a test's report carries the marks it was collected with; a module skipped at import has only its file's
        marks = _file_marks(self.config.rootpath / nodeid.split("::")[0])
        marks |= frozenset(name for name in ("extras", "postgres") if name in getattr(report, "keywords", {}))
        self.skips.append((nodeid, _reason(report), marks))

    def pytest_runtest_logreport(self, report) -> None:
        if report.when == "setup" or report.when == "call":
            self._record(report)

    def pytest_collectreport(self, report) -> None:
        self._record(report)

    def pytest_sessionfinish(self, session) -> None:
        mode = os.environ.get(GUARD_ENV, "")
        if not self.controller or not mode:
            return
        reporter = self.config.pluginmanager.get_plugin("terminalreporter")
        if mode not in GUARD_MODES:
            if reporter is not None:
                reporter.write_line(f"extras guard: {GUARD_ENV} must be one of {', '.join(GUARD_MODES)}, not {mode!r}")
            session.exitstatus = GUARD_FAILED
            return
        failed = unexcused(self.skips, mode)
        if reporter is not None:
            reporter.write_line(f"extras guard ({mode}): {len(self.skips)} skipped, {len(failed)} not excused")
            for line in failed:
                reporter.write_line(f"  {line} (mark it extras, or name its reason in ALLOWED_SKIPS)")
        if failed:
            session.exitstatus = GUARD_FAILED


def register(config) -> None:
    """Add the marks and the guard to this session, once."""
    if config.pluginmanager.has_plugin("extras_tests"):
        return
    for line in MARKERS:
        config.addinivalue_line("markers", line)
    config.pluginmanager.register(_Plugin(config), "extras_tests")
