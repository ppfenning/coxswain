"""Pull request land engine: wait for the required checks, squash-merge, mark the carried tasks done."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from agent_tools import forge_github, pr_checks, route_edit, run_store

LEVEL_MERGED = "level_merged"
MERGE_FAILED = "merge_failed"

_TASK_LINE = re.compile(r"^Task: (\S+)\s*$", re.M)
_TASKS_LINE = re.compile(r"^[ \t]*Tasks: (.+?)\s*$", re.M)
_PHASE_LINE = re.compile(r"^Phase: ", re.M)
_BULLET = re.compile(r"^- ([^\s:]+): ", re.M)


@dataclass(frozen=True)
class LandDeps:
    """The edges `land_pr` calls; `mark_done(task, url)` returns `(ok, text)` and `merge` returns `(merged, reason)`."""

    read_pr: Callable[[Path, int], Mapping | None]
    required_for_pr: Callable[[Path, int], tuple[str, ...] | None]
    check_rows: Callable[[Path, str], Sequence[Mapping[str, str]] | None]
    merge: Callable[[Path, int], tuple[bool, str]]
    mark_done: Callable[[str, str], tuple[bool, str]]
    sleep: Callable[[float], None]
    now: Callable[[], float]
    timeout_s: float
    poll_s: float


@dataclass(frozen=True)
class LandResult:
    """`event` is `level_merged` (with `url` and `tasks_marked`) or `merge_failed` (with `reason`); `text` is the line to print."""

    event: str
    text: str
    url: str | None = None
    tasks_marked: tuple[str, ...] = ()
    reason: str | None = None


def carried_tasks(body: str) -> tuple[str, ...]:
    """Task ids a PR body names, in order and once each: `Task:` lines, comma-separated `Tasks:` lines, and phase bullets above `Dropped:`."""
    singles = _TASK_LINE.findall(body)
    live = body.split("\nDropped:", 1)[0]
    grouped = [
        task
        for line in _TASKS_LINE.findall(live)
        for task in (part.strip() for part in line.split(","))
        if len(task.split()) == 1
    ]
    bullets = _BULLET.findall(live) if _PHASE_LINE.search(live) else []
    return tuple(dict.fromkeys([*singles, *grouped, *bullets]))


def _failed(reason: str) -> LandResult:
    return LandResult(MERGE_FAILED, f"merge failed: {reason}", reason=reason)


def _mark(deps: LandDeps, pr: Mapping) -> LandResult:
    """Mark each carried task done with the PR url; a task that cannot be marked is named in the text, not counted."""
    url = str(pr["url"])
    outcomes = [(task, *deps.mark_done(task, url)) for task in carried_tasks(str(pr.get("body", "")))]
    marked = tuple(task for task, ok, _ in outcomes if ok)
    misses = [f"{task}: {text}" for task, ok, text in outcomes if not ok]
    text = f"merged {url}; marked {len(marked)} task(s) done" + "".join(f"\nnot marked {miss}" for miss in misses)
    return LandResult(LEVEL_MERGED, text, url=url, tasks_marked=marked)


def _wait_ready(deps: LandDeps, repo: Path, required: tuple[str, ...], sha: str) -> tuple[bool, str]:
    """Poll the check rows until `ready` or `red` or the timeout; the reason names the checks that decided it."""
    # Edge bend (A2): `started` and `last` are rebound by the polling loop; the clock is injected.
    started, last = deps.now(), "no check rows read yet"
    while True:
        rows = deps.check_rows(repo, sha)
        verdict = pr_checks.verdict(required, rows) if rows is not None else None
        if verdict is not None and verdict.kind == pr_checks.READY:
            return True, verdict.reason
        if verdict is not None and verdict.kind not in (pr_checks.MISSING, pr_checks.PENDING):
            return False, verdict.reason
        last = "check rows could not be read" if verdict is None else verdict.reason
        if deps.now() - started >= deps.timeout_s:
            return False, f"timed out after {deps.timeout_s:g}s; {last}"
        deps.sleep(deps.poll_s)


def land_pr(repo: Path, number: int, deps: LandDeps) -> LandResult:
    """Wait for the base's required checks, squash-merge PR `number`, then mark its carried tasks done."""
    pr = deps.read_pr(repo, number)
    if pr is None:
        return _failed(f"pull request {number} could not be read")
    state = str(pr.get("state", "")).upper()
    if state == "MERGED":
        return _mark(deps, pr)
    if state != "OPEN":
        return _failed(f"pull request {number} is {state.lower() or 'in an unknown state'} and was not merged")
    if not carried_tasks(str(pr.get("body", ""))):
        return _failed(f"pull request {number} names no tasks, so there is nothing to mark done; it was not merged")
    required = deps.required_for_pr(repo, number)
    if not required:
        return _failed(f"pull request {number}: the required checks could not be read, or none are required")
    ready, reason = _wait_ready(deps, repo, required, str(pr["headRefOid"]))
    if not ready:
        return _failed(reason)
    merged, why = deps.merge(repo, number)
    return _mark(deps, pr) if merged else _failed(why)


def _store_mark_done(runs_dir: Path, at: str | None, task: str, url: str) -> tuple[bool, str]:
    """Edge: the one store row of `task`, moved to done with `url`; already done with the same PR counts as marked."""
    rows = [r for r in run_store.read_queue(runs_dir, kind="task") if r.get("task_id") == task]
    if len(rows) != 1:
        return False, f"{len(rows)} store rows for {task}"
    result = route_edit.mark_done_with_pr(runs_dir, rows[0], url, at)
    if result is None:
        return True, f"{task} marked done"
    return result.code == route_edit.DONE, result.text


def real_deps(runs_dir: Path, timeout_s: float, poll_s: float, at: str | None = None) -> LandDeps:
    """Edge: the forge readers and writers, the monotonic clock, and the store's mark-done."""
    return LandDeps(
        read_pr=forge_github.read_pr,
        required_for_pr=forge_github.required_for_pr,
        check_rows=forge_github.pr_check_rows,
        merge=forge_github.merge_pr,
        mark_done=lambda task, url: _store_mark_done(runs_dir, at, task, url),
        sleep=time.sleep,
        now=time.monotonic,
        timeout_s=timeout_s,
        poll_s=poll_s,
    )
