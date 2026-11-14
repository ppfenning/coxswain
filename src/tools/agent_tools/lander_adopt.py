"""Adopt: bring an initiative that was in flight before the landing record existed under the landing model.

One step per initiative, and safe to run twice: an initiative that has a record is left alone. It works from each
task's stored `merged_commit` and from branch ancestry, never from run records, so an approval made in any run counts.

For each task, in this order:

    its work is already on the parent (same commit, or the same patch)  ->  done
    approved, its commit on its phase branch                            ->  kept
    approved, its commit not on the phase branch but in this clone      ->  carried: merged into the phase branch
    approved, its commit nowhere in this clone                          ->  back to ready, with the reason

Then the initiative: its branch is kept (`pr/<id>` from an earlier release) or cut from the parent, the record is
made, and every built phase is merged in dependency order. Nothing is ever deleted, reset or rebased here, so no
recorded commit can be lost: one that is not merged stays on the branch it was on.

`plan` only reads. `apply` performs a plan. The dry run is `plan` alone.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_tools import lander
from core import landing_record as lr
from core.landing import TemplateError, initiative_branch_name, is_initiative, pr_branch_name, resolve_land_at
from harness import landing_git as lg
from harness import landing_ops as ops
from harness import landing_store as ls
from harness.base_sync import holds


@dataclass(frozen=True)
class TaskFinding:
    task: str
    phase: str
    action: str  # done, keep, carry, ready, none
    why: str = ""
    commit: str = ""


@dataclass(frozen=True)
class Plan:
    """What adopting one initiative would do. `skip` says why nothing would be done."""

    initiative: str
    skip: str = ""
    branch: str = ""
    branch_exists: bool = False
    parent: str = ""
    tasks: tuple[TaskFinding, ...] = field(default_factory=tuple)
    merge: tuple[str, ...] = field(default_factory=tuple)  # phases that will be merged, in order
    waiting: tuple[tuple[str, str], ...] = field(default_factory=tuple)  # (phase, why it is not merged now)
    landed: bool = False  # every task is done: only the record is written

    def lines(self) -> list[str]:
        if self.skip:
            return [f"{self.initiative}: skipped: {self.skip}"]
        cut = "kept" if self.branch_exists else f"cut from {self.parent}"
        out = [f"{self.initiative}: branch {self.branch} ({cut}), parent {self.parent}" + (", already landed" if self.landed else "")]
        out += [f"  task {t.task} ({t.phase}): {t.action}" + (f": {t.why}" if t.why else "") for t in self.tasks if t.action != "none"]
        out += [f"  phase {p}: merge into {self.branch}" for p in self.merge]
        out += [f"  phase {p}: not merged now: {why}" for p, why in self.waiting]
        return out


def find_task(repo: Path, task: Mapping[str, Any], phase_branch: str, parent_ref: str) -> TaskFinding:
    """Edge (reads git). What adopt does with one task."""
    ident, phase, state = str(task["id"]), str(task.get("phase") or ""), str(task.get("state"))
    commit = str(task.get("merged_commit") or "")
    if state != "approved":
        return TaskFinding(ident, phase, "none")
    if commit and holds(repo, commit, parent_ref):
        return TaskFinding(ident, phase, "done", f"its work is already on {parent_ref}", commit)
    tip = lg.rev(repo, f"refs/heads/{phase_branch}")
    if commit and tip and lg.is_ancestor(repo, commit, tip):
        return TaskFinding(ident, phase, "keep", "", commit)
    if commit and lg.rev(repo, commit):
        return TaskFinding(ident, phase, "carry", f"{commit[:12]} is not on {phase_branch}; it is merged into it", commit)
    if not commit and tip:
        return TaskFinding(ident, phase, "keep", "no commit is recorded; the phase branch is taken as it stands")
    missing = f"commit {commit[:12]} is not in this clone" if commit else f"no commit is recorded and {phase_branch} does not exist"
    return TaskFinding(ident, phase, "ready", f"{missing}; the approval cannot be found, so the task builds again")


def _after(tasks: Sequence[Mapping[str, Any]], findings: Sequence[TaskFinding]) -> list[dict[str, Any]]:
    """The task rows as they will read once the findings are applied."""
    new_state = {f.task: {"done": "done", "ready": "ready"}.get(f.action) for f in findings}
    return [{**t, "state": new_state.get(str(t["id"])) or t.get("state")} for t in tasks]


def plan(ports: lander.Ports, initiative: str) -> Plan:
    """Reads only. What adopting `initiative` would do on this machine."""
    if ls.get_record(ports.conn, initiative) is not None:
        return Plan(initiative, skip="it has a landing record already")
    tasks, meta = ports.tasks(initiative), ports.meta(initiative)
    if not tasks:
        return Plan(initiative, skip="it has no tasks")
    own, cartridge = meta.get("initiative") or {"id": initiative}, meta.get("cartridge") or {}
    try:
        land_at = resolve_land_at(cartridge, own.get("land_at"))
    except ValueError as exc:
        return Plan(initiative, skip=str(exc))
    if not is_initiative(land_at):
        return Plan(initiative, skip="it lands at task, which keeps no landing record")
    try:
        repo = lander._repo(ports, {"initiative": initiative, "repo": own.get("repo") or ""})
    except LookupError as exc:
        return Plan(initiative, skip=str(exc))
    if lg.rev(repo, "HEAD") is None:
        return Plan(initiative, skip=f"its repository is not at {repo}")
    parent = str(meta.get("target_branch") or own.get("target_branch") or _default_branch(repo))
    parent_ref = lander._parent_ref(repo, parent)
    legacy = pr_branch_name(initiative)
    if lg.branch_ref(repo, legacy) is not None:
        branch = legacy
    else:
        try:
            branch = initiative_branch_name(cartridge, own, parent, meta.get("epic_issue"))
        except TemplateError as exc:
            return Plan(initiative, skip=str(exc))
    findings = tuple(find_task(repo, t, ops.phase_branch(initiative, str(t.get("phase") or "")), parent_ref) for t in tasks)
    rows = _after(tasks, findings)
    if all(str(t.get("state")) in lr.LANDED_TASK_STATES for t in rows):
        return Plan(initiative, branch=branch, branch_exists=lg.branch_ref(repo, branch) is not None, parent=parent,
                    tasks=findings, landed=True)
    parents = lr.phase_parents(rows)
    merge: list[str] = []
    waiting: list[tuple[str, str]] = []
    for phase in lr.merge_order(parents):
        own_rows = lr.phase_tasks(rows, phase)
        states = [str(t.get("state")) for t in own_rows]
        if all(s in lr.LANDED_TASK_STATES for s in states):
            continue
        unmerged = [p for p in parents.get(phase, ()) if p in dict(waiting)]
        if unmerged:
            waiting.append((phase, f"parent phase {unmerged[0]} is not merged"))
        elif "quarantined" in states:
            waiting.append((phase, "a task is quarantined; on_blocked applies once the record exists"))
        elif not all(s in lr.SETTLED_TASK_STATES for s in states):
            open_ids = ", ".join(str(t["id"]) for t in own_rows if str(t.get("state")) not in lr.SETTLED_TASK_STATES)
            waiting.append((phase, f"task(s) {open_ids} are still to build; it is built on its branch as it stands"))
        else:
            merge.append(phase)
    return Plan(initiative, branch=branch, branch_exists=lg.branch_ref(repo, branch) is not None, parent=parent,
                tasks=findings, merge=tuple(merge), waiting=tuple(waiting))


def _default_branch(repo: Path) -> str:
    ok, out = lg.git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if ok and out.startswith("origin/"):
        return out.removeprefix("origin/")
    return "main" if lg.rev(repo, "refs/heads/main") else (lg.git(repo, "branch", "--show-current")[1] or "main")


def apply(ports: lander.Ports, found: Plan, send_back: Callable[[str, str, str], str | None]) -> list[str]:
    """Perform a plan. Each step reads what is already so, and a step that fails stops the rest of this initiative.

    `send_back(initiative, task, reason)` moves an approved task whose work cannot be found back to ready; it returns
    why it was refused, or None."""
    initiative = found.initiative
    if found.skip:
        return found.lines()
    if ls.get_record(ports.conn, initiative) is not None:
        return [f"{initiative}: skipped: it has a landing record already"]
    repo = lander._repo(ports, {"initiative": initiative, "repo": (ports.meta(initiative).get("initiative") or {}).get("repo") or ""})
    events: list[str] = []
    for finding in found.tasks:
        if finding.action == "done":
            why = ports.mark_done(initiative, finding.task, "")
            events.append(f"{initiative}: {finding.task} marked done ({finding.why})" if why is None
                          else f"{initiative}: {finding.task} not marked done: {why}")
        elif finding.action == "ready":
            why = send_back(initiative, finding.task, finding.why)
            events.append(f"{initiative}: {finding.task} sent back to ready ({finding.why})" if why is None
                          else f"{initiative}: {finding.task} not sent back: {why}")
        elif finding.action == "carry":
            branch = ops.phase_branch(initiative, finding.phase)
            tip, _, why = lg.ensure_branch(repo, branch, finding.commit)
            carried = lg.merge_into(repo, branch=branch, source=finding.commit,
                                    message=f"{initiative}: carry {finding.task} onto its phase branch") if tip else None
            if carried is None or not carried.ok:
                detail = why if carried is None else carried.detail
                return [*events, f"{initiative}: stopped: {finding.task} could not be carried onto {branch}: {detail}"]
            events.append(f"{initiative}: {finding.task} carried onto {branch}")
    record, why = ops.start_record(ports.conn, repo, initiative=initiative, branch=found.branch, parent=found.parent,
                                   parent_ref=lander._parent_ref(repo, found.parent), now=ports.now(), adopted=True)
    if record is None:
        return [*events, f"{initiative}: stopped: no record was made: {why}"]
    events.append(f"{initiative}: record made on {record['branch']}")
    if found.landed:
        ls.put_record(ports.conn, lr.with_state({**record, "state": "assembled"}, "landed", ports.now()))
        return [*events, f"{initiative}: recorded landed"]
    for phase in found.merge:
        outcome = lander.merge_phase(ports, initiative, phase)
        events += list(outcome.events) or [f"{initiative}: phase {phase}: {outcome.status}: {outcome.detail}"]
        if outcome.status != "done":
            break  # a later phase may need this one; the lander's own passes take it from here
    return events


def candidates(ports: lander.Ports, initiatives: Sequence[str]) -> list[str]:
    """The initiatives with no landing record, in the order given."""
    have = {str(r["initiative"]) for r in ls.list_records(ports.conn)}
    return [i for i in initiatives if i not in have]
