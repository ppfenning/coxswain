#!/usr/bin/env python3
"""Pure summary of `cox route status --json` for the coxswain statusline.

Assumes the CLI reports a JSON array of run objects, each carrying a
`state` key and, once it has spent anything, a `usage.cost_usd` number and
a UTC `started_at` timestamp shaped "YYYY-MM-DDTHH:MM:SS" (a trailing `Z`
or fractional seconds are tolerated and ignored). A run whose `started_at`
does not parse that way is excluded from the spend total rather than
guessed at, and is reported back in `unparsed` so a caller can act on it.
"""
import calendar
import json
import re
import sys
import time
from typing import NamedTuple, Optional, Sequence


class Summary(NamedTuple):
    running: int
    slots: int
    spend: Optional[float]
    unparsed: tuple


def _parsed_utc(stamp: str) -> Optional[float]:
    try:
        return float(calendar.timegm(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, TypeError, OverflowError):
        return None


def summarize(runs: Sequence[dict], slots: int, now: float) -> Summary:
    running = sum(1 for run in runs if run.get("state") == "running")
    recent_costs = []
    unparsed = []
    for run in runs:
        usage = run.get("usage") or {}
        # `usage` is the harness's one-line string ("usage   : 17 node call(s), ... $4.51 — ...")
        # in `cox route status --json`; a dict form is accepted too.
        if isinstance(usage, str):
            found = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", usage)
            cost = float(found.group(1)) if found else None
        else:
            cost = usage.get("cost_usd")
        if cost is None:
            continue
        started = run.get("started_at")
        if not started:
            continue
        parsed = _parsed_utc(started)
        if parsed is None:
            unparsed.append(started)
            continue
        if now - parsed > 5 * 3600:
            continue
        recent_costs.append(cost)
    spend = sum(recent_costs) if recent_costs else None
    return Summary(running=running, slots=slots, spend=spend, unparsed=tuple(unparsed))


def main(argv):
    runs = json.loads(argv[1])
    slots = int(argv[2])
    summary = summarize(runs, slots, time.time())
    print(summary.running)
    print(summary.slots)
    print(f"{summary.spend:.2f}" if summary.spend is not None else "")


if __name__ == "__main__":
    main(sys.argv)
