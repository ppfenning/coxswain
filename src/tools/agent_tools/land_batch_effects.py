"""The edge that builds the real Effects for the merge-queue driver.

The shaping functions are pure: records in, source mappings out. `build_effects` is the only part that reads or runs
anything, and it does so through callables it is handed, so this module never imports `cli`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from agent_tools.land import _phase_approved_files
from agent_tools.land_batch import batch_plan
from agent_tools.land_batch_group import batch_key
from agent_tools.land_batch_run import Effects, Outcome, Source

PhaseRead = tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, str]]


def parse_source(text: str) -> tuple[str, str, str] | None:
    """`initiative/phase/run` as a triple, None when it is not exactly three non-empty parts."""
    parts = text.split("/")
    return (parts[0], parts[1], parts[2]) if len(parts) == 3 and all(parts) else None


def ready_source(initiative: str, phase: str, run: str, repo: str, items: Sequence[Mapping[str, Any]],
                 records: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """The source for a phase whose items are all done or approved and at least one approved, else None.

    `files` is the sorted union of the approved tasks' `build.files_touched`; empty means unknown to the grouping rule.
    """
    approved = [str(i["id"]) for i in items if i.get("status") == "approved"]
    if not approved or any(i.get("status") not in ("done", "approved") for i in items):
        return None
    named = _phase_approved_files(list(items), records)
    files = sorted({f for entry in named for f in entry["files_touched"]})
    return {"initiative": initiative, "phase": phase, "run": run, "repo": repo, "files": files, "tasks": approved}


def ticket_paths(items: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Approved task id to its ticket file, for `mark_done` to close out."""
    return {str(i["id"]): str(i["file"]) for i in items if i.get("status") == "approved"}


def build_effects(
    refs: Sequence[tuple[str, str, str]],
    repo: str,
    read_phase: Callable[[str, str, str], PhaseRead | None],
    run_steps: Callable[[list[dict[str, Any]], Mapping[str, str]], tuple[str | None, str]],
    repo_facts: dict[str, Any],
    default_branch: str,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> Effects:
    """`read_phase` gives a phase's items, records and task record paths; `run_steps` gives an error or None, and the pr.

    Bends A3: `paths` and `tickets` are filled by `read_sources` and read by `land`, because the driver hands `land`
    only the sources. They are scoped to this closure.
    """
    paths: dict[str, str] = {}
    tickets: dict[str, str] = {}

    def read_sources() -> list[Source]:
        read = [(ref, read_phase(*ref)) for ref in refs]
        shaped = [(ready_source(*ref, repo, got[0], got[1]), got) for ref, got in read if got is not None]
        for source, got in shaped:
            if source is not None:
                paths.update({t: p for t, p in got[2].items() if t in source["tasks"]})
                tickets.update({t: p for t, p in ticket_paths(got[0]).items() if t in source["tasks"]})
        return [s for s, _ in shaped if s is not None]

    def land(batch: list[Source]) -> Outcome:
        steps = batch_plan(batch, repo_facts, default_branch, paths, batch_key(batch))
        error, pr = run_steps(steps, tickets)
        return Outcome(error, pr or None)

    return Effects(read_sources, land, sleep, now)
