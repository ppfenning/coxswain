"""The edge that builds the real `Effects` for the merge-queue driver.

The shaping functions are pure: records in, source mappings out. `make_effects` only composes injected readers and the
injected step runner, so the walk, the lease and the forge stay in cli.py.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from agent_tools.land import _phase_approved_files
from agent_tools.land_batch import batch_plan
from agent_tools.land_batch_group import batch_key
from agent_tools.land_batch_run import Effects, Outcome

Source = dict[str, Any]
RunSteps = Callable[[list[dict[str, Any]], list[Source]], tuple[int, list[str], str]]


def phase_ready(items: Sequence[Mapping[str, Any]]) -> bool:
    """Every task is done or approved, and at least one is approved."""
    states = [item.get("status") for item in items]
    return "approved" in states and all(s in ("done", "approved") for s in states)


def source_tasks(items: Sequence[Mapping[str, Any]]) -> list[str]:
    return [item["id"] for item in items if item.get("status") == "approved"]


def source_files(items: list[dict[str, Any]], records: dict[str, dict[str, Any]]) -> list[str]:
    """Files the approved tasks touched, first seen first. Empty when no task names one, which grouping reads as unknown."""
    named = [f for entry in _phase_approved_files(items, records) for f in entry["files_touched"]]
    return list(dict.fromkeys(named))


def build_source(initiative: str, phase: str, run: str, repo: str, items: list[dict[str, Any]],
                 task_records: Sequence[Mapping[str, Any]], task_paths: Mapping[str, str]) -> Source:
    tasks = source_tasks(items)
    return {
        "initiative": initiative, "phase": phase, "run": run, "repo": repo,
        "files": source_files(items, {r["task"]: dict(r) for r in task_records}),
        "tasks": tasks,
        "task_paths": {t: task_paths[t] for t in tasks if t in task_paths},
        "task_items": {i["id"]: i["file"] for i in items if i.get("status") == "approved" and i.get("file")},
    }


def parse_source_ids(values: Sequence[str]) -> tuple[list[tuple[str, str, str]] | None, str | None]:
    """`initiative/phase/run` strings to tuples, None when none were given, or the reason one is malformed."""
    parts = [(v, v.split("/")) for v in values]
    bad = next((v for v, p in parts if len(p) != 3 or not all(p)), None)
    if bad is not None:
        return None, f"--source {bad!r} is not initiative/phase/run"
    return ([(p[0], p[1], p[2]) for _, p in parts] or None), None


def plan_error(steps: Sequence[Mapping[str, Any]]) -> str | None:
    """The reason a plan refused to start, else None. A refused plan runs no step."""
    return steps[0]["reason"] if steps and steps[0]["kind"] == "refuse" else None


def outcome_of(rc: int, reached: Sequence[str], pr: str) -> Outcome:
    """`pr` is the reference the walk took from the `pr_create` step. A walk that exited non-zero is an error."""
    if rc == 0:
        return Outcome(None, pr or None)
    return Outcome(f"land stopped after {reached[-1] if reached else 'no step'} (exit {rc})", pr or None)


def make_effects(read_sources: Callable[[], list[Source]], run_steps: RunSteps, repo_facts: dict[str, Any],
                 default_branch: str, stamp: Callable[[], str],
                 sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], float] = time.monotonic) -> Effects:
    """`stamp` keeps a retried batch off the branch a stopped one left behind."""

    def land(batch: list[Source]) -> Outcome:
        paths = {t: p for s in batch for t, p in s["task_paths"].items()}
        steps = batch_plan(batch, repo_facts, default_branch, paths, f"{batch_key(batch)}-{stamp()}")
        refused = plan_error(steps)
        if refused is not None:
            return Outcome(refused, None)
        return outcome_of(*run_steps(steps, batch))

    return Effects(read_sources, land, sleep, now)
