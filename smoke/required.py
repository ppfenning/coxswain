"""Under `COX_SMOKE_REQUIRED`, a skipped smoke test or module fails the run.

`smoke/conftest.py` takes these two hooks in, and `smoke/test_smoke_required.py` loads this module with `-p` into a
pytest of its own to prove them. A skip anywhere counts: a test's `pytest.skip`, a `skipif` mark, a fixture's skip,
and a module's `pytest.importorskip` at collection, which no fixture sees. Only a reason in
`plan.ALLOWED_SKIPS` stands; an xfail, which pytest also reports as skipped, is left alone.
"""

from __future__ import annotations

import os

import pytest

from smoke import plan


def _message(report: pytest.CollectReport | pytest.TestReport) -> str:
    """The skip's message: a skip report's longrepr is (path, line, message)."""
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2])
    return str(longrepr)


def _refused(report: pytest.CollectReport | pytest.TestReport) -> None:
    if not report.skipped or hasattr(report, "wasxfail"):
        return
    refusal = plan.skip_refusal(_message(report), required=plan.is_required(os.environ.get(plan.REQUIRED_ENV)))
    if refusal:
        report.outcome = "failed"
        report.longrepr = refusal


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):
    report = yield
    _refused(report)
    return report


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector: pytest.Collector):
    report = yield
    _refused(report)
    return report
