"""Pure: which tests needed a serial rerun often enough to be called flaky, from task records."""

from __future__ import annotations

import datetime
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = ["FLAKY_DAYS", "FLAKY_RERUNS", "flaky_tests", "render_lines"]

FLAKY_RERUNS = 3  # serial reruns that make a test flaky
FLAKY_DAYS = 7  # within this many days, today included


def flaky_tests(
    dated_records: Iterable[tuple[str, Mapping[str, Any]]],
    today: datetime.date,
    days: int = FLAKY_DAYS,
    reruns: int = FLAKY_RERUNS,
) -> dict[str, int]:
    """Test id to serial-rerun count, for ids rerun `reruns` or more times in the `days` ending `today`.

    Each `(YYYY-MM-DD, task record)` counts a test once per record, from the `flaky` ids of
    `change_facts["checks"]`. Most reruns first, then by id.
    """
    since = (today - datetime.timedelta(days=days - 1)).isoformat()
    counts: Counter[str] = Counter(
        test_id
        for date, record in dated_records
        if since <= date <= today.isoformat()
        for test_id in {
            i
            for check in (record.get("change_facts") or {}).get("checks") or ()
            if isinstance(check, Mapping)
            for i in check.get("flaky") or ()
        }
    )
    return dict(sorted(((i, n) for i, n in counts.items() if n >= reruns), key=lambda kv: (-kv[1], kv[0])))


def render_lines(flaky: Mapping[str, int], days: int = FLAKY_DAYS) -> list[str]:
    if not flaky:
        return [f"no test needed a serial rerun {FLAKY_RERUNS} times in {days} days"]
    return [f"flaky (serial reruns in {days} days):", *(f"  {n}  {test_id}" for test_id, n in flaky.items())]
