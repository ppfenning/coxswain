"""Drive one merge-queue batch: collect, open, bisect, defer.

The driver orchestrates the pure grouping, bisection and open rules through injected effects and does no I/O itself.
`land` builds the plan with `land_batch.batch_plan` and runs it as one pull request on one CI run. Only the first
batch opens in a pass; every other source is deferred untouched, so the next chair tick plans it again.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from agent_tools.land_batch_bisect import bisect_land
from agent_tools.land_batch_group import disjoint_batches
from agent_tools.land_batch_settings import MergeQueue, should_open

Source = Mapping[str, Any]


@dataclass(frozen=True)
class Outcome:
    error: str | None
    pr: str | None


@dataclass(frozen=True)
class Effects:
    """`read_sources` lists the sources ready to land; `now` is monotonic seconds; `land` runs one batch as one PR."""

    read_sources: Callable[[], list[Source]]
    land: Callable[[list[Source]], Outcome]
    sleep: Callable[[float], None]
    now: Callable[[], float]


@dataclass(frozen=True)
class QueueResult:
    landed: list[tuple[Source, str | None]]
    returned: list[tuple[Source, str]]
    deferred: list[Source]
    ci_runs: int


def _label(source: Source) -> str:
    return f"{source['initiative']}/{source['phase']}"


def _collect(settings: MergeQueue, effects: Effects, poll_s: float) -> tuple[list[Source], list[Source]] | None:
    """The batch to open and every source read, or None when a pass finds no sources.

    Bends A2/A3: the wait is a loop over injected effects, the edge of the driver, so it re-binds `sources`.
    """
    started = effects.now()
    while True:
        sources = effects.read_sources()
        if not sources:
            return None
        batch = disjoint_batches(sources, settings.batch_size)[0]
        if should_open(len(batch), effects.now() - started, settings):
            return batch, sources
        effects.sleep(poll_s)


def run_queue(settings: MergeQueue, effects: Effects, poll_s: float) -> QueueResult:
    collected = _collect(settings, effects, poll_s)
    if collected is None:
        return QueueResult([], [], [], 0)
    batch, sources = collected
    prs: dict[str, str | None] = {}

    def land(group: list[Source]) -> str | None:
        # The one mutation: a record of the pr each successful call gave its sources, scoped to this wrapper.
        outcome = effects.land(group)
        if outcome.error is None:
            prs.update({_label(s): outcome.pr for s in group})
        return outcome.error

    result = bisect_land(batch, land)
    opened = {_label(s) for s in batch}
    return QueueResult(
        [(s, prs[_label(s)]) for s in result.landed],
        list(result.returned),
        [s for s in sources if _label(s) not in opened],
        result.ci_runs,
    )


def _first_line(detail: str) -> str:
    return next(iter(detail.splitlines()), "")


def render_lines(result: QueueResult) -> str:
    """One line per source, then the summary. A landed source with no pr renders `pr=` empty."""
    lines = [
        *(f"batch landed {_label(s)} pr={pr or ''}" for s, pr in result.landed),
        *(f"batch returned {_label(s)}: {_first_line(d)}" for s, d in result.returned),
        *(f"batch deferred {_label(s)}" for s in result.deferred),
        f"batch summary: ci_runs={result.ci_runs} landed={len(result.landed)} "
        f"returned={len(result.returned)} deferred={len(result.deferred)}",
    ]
    return "\n".join(lines) + "\n"
