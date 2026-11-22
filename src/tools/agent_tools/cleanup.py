"""Clean up after a harness run: its worktrees and scratch branches, never its phase branches.

Pure planning, then one edge that runs git. Dry-run is the default; nothing is
removed unless asked. A phase branch (`epic/<initiative>/<phase>`) is what the
next run stacks on and is never touched; the task branches
(`agents/<run-id>/<task>`) and the private merge targets
(`epic/<initiative>/<phase>--<task>`) belong to a finished run and go.

Only the run's own: `owned_branches` names them from the run id and the run's
task records, so another initiative's `epic/*--*` branch is never in a plan,
and `--force` has nothing of another initiative's to widen to.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_tools import bounded_run, epic
from agent_tools.chair_facts_read import run_initiative

__all__ = ["apply_cleanup", "git_branches", "git_worktrees", "live_owner", "owned_branches", "plan_cleanup", "run_records"]


def _git(repo: Path, *args: str) -> str:
    return bounded_run.completed(["git", "-C", str(repo), *args], capture_output=True, text=True).stdout


def git_worktrees(repo: Path) -> list[str]:
    return [line.split(" ", 1)[1] for line in _git(repo, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")]


def git_branches(repo: Path) -> list[str]:
    return [line.lstrip("*+ ").strip() for line in _git(repo, "branch", "--list").splitlines() if line.strip()]


def live_owner(runs_dir: Path | str, run_id: str) -> dict[str, Any] | None:
    """Edge. `{"pid", "state"}` when the run's pidfile names a process that is running or fenced, else None."""
    pidfile = Path(runs_dir) / f"{run_id}.pid"
    try:
        text = pidfile.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.isdigit():
        return None
    state = epic.run_state(int(text), pidfile)
    return {"pid": int(text), "state": state} if state in ("running", "fenced") else None


def _task_of(branch: str) -> str | None:
    """The task name behind a doomed branch: third segment of `agents/<run>/<task>`,
    or the text after the last `--` of `epic/<init>/<phase>--<task>`."""
    if branch.startswith("agents/") and branch.count("/") >= 2:
        return branch.split("/", 2)[2]
    if branch.startswith("epic/") and "--" in branch:
        return branch.rsplit("--", 1)[1]
    return None


def run_records(tasks_dir: Path) -> list[dict[str, Any]]:
    """Edge. The run's task records under `runs/<run>/tasks/<phase>/<task>.json`, each carrying the `phase` and `task`
    its path names unless the record names its own. A file that is not a JSON object is no record."""
    out = []
    for f in sorted(tasks_dir.glob("*/*.json")):
        record = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(record, dict):
            out.append({"phase": f.parent.name, "task": f.stem, **record})
    return out


def _initiative_of(run_id: str, record: Mapping[str, Any]) -> str:
    """The record's own `initiative`, else the run id's: `x-5` is `x`, as `chair_facts.run_initiative` takes it."""
    named = record.get("initiative")
    return named if isinstance(named, str) and named else run_initiative(run_id)


def owned_branches(run_id: str, records: Sequence[Mapping[str, Any]], branches: Sequence[str]) -> list[str]:
    """Pure: the branches in `branches` the run `run_id` owns, in their order. Its scratch namespace
    `agents/<run_id>/...`, and for each of its task records the private merge target the record names in `draft`,
    and `epic/<initiative>/<phase>--<task>` with the record's initiative, else the run id's. Nothing else: a branch
    of another initiative, or one no record of this run names, is never the run's."""
    if not run_id:
        return []
    named: set[str] = set()
    for r in records:
        draft, phase, task = r.get("draft"), r.get("phase"), r.get("task")
        if isinstance(draft, str) and draft.startswith("epic/") and "--" in draft:
            named.add(draft)
        if phase and task:
            named.add(f"epic/{_initiative_of(run_id, r)}/{phase}--{task}")
    return [b for b in branches if b.startswith(f"agents/{run_id}/") or b in named]


def plan_cleanup(*, run_id: str, worktrees: Sequence[str], branches: Sequence[str], worktree_root: str,
                 records: Sequence[Mapping[str, Any]], live: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pure: what a run left behind, from its id and its task `records`: `owned_branches`, and nothing else.
    The phase branches of the run's initiative are listed as kept, explicitly.
    A `live` owner (pid and state) empties the plan and adds a `skipped` line."""
    if live is not None:
        skipped = f"skipped run {run_id}: pid {live['pid']} is {live['state']}"
        return {"run_id": run_id, "worktrees": [], "branches": [], "kept_phase_branches": [], "root": None, "skipped": skipped}
    root = str(Path(worktree_root).expanduser()) + "/" + run_id
    doomed_worktrees = [w for w in worktrees if w.startswith(root + "/") or w == root]
    doomed_branches = owned_branches(run_id, records, branches)
    initiatives = {_initiative_of(run_id, r) for r in records} | {run_initiative(run_id)}
    kept = [b for b in branches if "--" not in b and any(b.startswith(f"epic/{i}/") for i in initiatives)]
    return {"run_id": run_id, "worktrees": doomed_worktrees, "branches": doomed_branches, "kept_phase_branches": kept, "root": root}


def apply_cleanup(repo: Path | str, plan: dict[str, Any], *, dry_run: bool = True, landed: Sequence[str] = (),
                   dropped: Sequence[str] = (), reasons: dict[str, str] | None = None, force: bool = False) -> list[str]:
    """The edge. Returns what was (or would be) done, one line each. A branch
    whose task is neither `landed` nor `dropped` is a draft and is kept,
    printed with its stop reason, unless `force`. The draft is the agents branch;
    a recorded task's `epic/...--<task>` is a private merge target and goes.
    `force` reaches only the plan's branches, which are the run's own (`owned_branches`)."""
    if plan.get("skipped"):
        return [plan["skipped"]]
    repo = Path(repo)
    reasons = reasons or {}
    lines = []
    for w in plan["worktrees"]:
        lines.append(f"{'would remove' if dry_run else 'removed'} worktree {w}")
        if not dry_run:
            bounded_run.completed(["git", "-C", str(repo), "worktree", "remove", "--force", w], capture_output=True)
    if not dry_run:
        bounded_run.completed(["git", "-C", str(repo), "worktree", "prune"], capture_output=True)
    for b in plan["branches"]:
        task = _task_of(b)
        settled = task in landed or task in dropped
        private_target = b.startswith("epic/") and task in reasons
        if not settled and not force and not private_target:
            lines.append(f"kept draft branch {b}: {reasons.get(task, 'unlanded')}")
            continue
        suffix = " (forced)" if force and not settled else ""
        lines.append(f"{'would delete' if dry_run else 'deleted'} branch {b}{suffix}")
        if not dry_run:
            bounded_run.completed(["git", "-C", str(repo), "branch", "-D", b], capture_output=True)
    root = Path(plan["root"])
    if root.exists():
        lines.append(f"{'would remove' if dry_run else 'removed'} directory {root}")
        if not dry_run:
            import shutil
            shutil.rmtree(root, ignore_errors=True)
    for b in plan["kept_phase_branches"]:
        lines.append(f"kept phase branch {b}")
    return lines
