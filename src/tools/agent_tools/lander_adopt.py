"""Adopt: bring an initiative that was in flight before the landing record existed under the landing model.

One step per initiative, and safe to run twice: an initiative that has a record is left alone. It works from each
task's stored `merged_commit` and from branch ancestry, never from run records, so an approval made in any run counts.

For each task, in this order:

    its work is already on the parent (same commit, or the same patch)  ->  done
    approved, its commit on its phase branch, here or on origin         ->  kept
    approved, its commit not on the phase branch but in this clone      ->  carried: merged into the phase branch
    approved, its commit nowhere in this clone                          ->  back to ready, with the reason

Then the initiative: its branch is kept (`pr/<id>` from an earlier release) or cut from the parent, the record is
made (stamped with the initiative's `lane_model`, so the lander knows which model it merges), and every built phase is
merged in dependency order. Nothing is ever deleted, reset or rebased here, so no
recorded commit can be lost: one that is not merged stays on the branch it was on.

`plan` only reads. `apply` performs a plan. The dry run is `plan` alone. Before either, `refresh` fetches origin's
phase branches of the initiative into their remote-tracking refs only: origin holds the code, so a task is judged
against origin's copy as well as this clone's, and no local branch or work tree is touched.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from agent_tools import lander
from agent_tools.lander_base import publish_initiative
from core import landing_record as lr
from core.landing import TemplateError, initiative_branch_name, is_initiative, pr_branch_name, resolve_land_at
from harness import bounded_proc, initiative_review, store_phases
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


def find_task(repo: Path, task: lander.Row, phase_branch: str, parent_ref: str) -> TaskFinding:
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
    origin_tip = lg.rev(repo, f"refs/remotes/origin/{phase_branch}")
    if commit and origin_tip and lg.is_ancestor(repo, commit, origin_tip):
        # The phase merge fetches origin's phase branch before it merges, so origin's copy is the one merged.
        return TaskFinding(ident, phase, "keep", f"it is on origin's {phase_branch}", commit)
    if commit and lg.rev(repo, commit):
        return TaskFinding(ident, phase, "carry", f"{commit[:12]} is not on {phase_branch}; it is merged into it", commit)
    if not commit and tip:
        return TaskFinding(ident, phase, "keep", "no commit is recorded; the phase branch is taken as it stands")
    missing = f"commit {commit[:12]} is not in this clone" if commit else f"no commit is recorded and {phase_branch} does not exist"
    return TaskFinding(ident, phase, "ready", f"{missing}; the approval cannot be found, so the task builds again")


FETCH_TIMEOUT_S = 120.0


def phase_refspec(initiative: str) -> str:
    """Pure. Origin's phase branches of `initiative` onto their remote-tracking refs, and nothing else."""
    branches = ops.phase_branch(initiative, "*")
    return f"+refs/heads/{branches}:refs/remotes/origin/{branches}"


def refresh(ports: lander.Ports, initiative: str, timeout: float = FETCH_TIMEOUT_S) -> list[str]:
    """Edge. Fetch origin's phase branches of `initiative` into `refs/remotes/origin/epic/<initiative>/*`, bounded by
    `timeout`. No local branch, work tree or tag moves. A repo that is not known, or has no origin, has nothing to
    fetch. A failed fetch is reported, never raised: the plan is made against the clone as it stands."""
    try:
        repo = lander.ports_repo(ports, {"initiative": initiative,
                                         "repo": str((ports.meta(initiative).get("initiative") or {}).get("repo") or "")})
    except LookupError:
        return []  # `plan` says why it skips
    if lg.rev(repo, "HEAD") is None or not lg.git(repo, "remote", "get-url", "origin")[0]:
        return []
    done = bounded_proc.completed(["git", "-C", str(repo), "fetch", "--no-tags", "--quiet", "--no-write-fetch-head",
                                   "origin", phase_refspec(initiative)], timeout=timeout, capture_output=True, text=True)
    if done.returncode == 0:
        return []
    said = (done.stderr or done.stdout or f"exit {done.returncode}").strip().splitlines()
    return [f"{initiative}: origin's phase branches could not be fetched ({said[0] if said else 'no output'}); "
            "planned against this clone as it stands"]


def _after(tasks: Sequence[lander.Row], findings: Sequence[TaskFinding]) -> list[dict[str, object]]:
    """The task rows as they will read once the findings are applied."""
    new_state = {f.task: {"done": "done", "ready": "ready"}.get(f.action) for f in findings}
    return [{**t, "state": new_state.get(str(t["id"])) or t.get("state")} for t in tasks]


def plan(ports: lander.Ports, initiative: str) -> Plan:
    """Reads only. What adopting `initiative` would do on this machine."""
    present = present_skip(ls.read_record(ports.conn, initiative))
    if present is not None:
        return Plan(initiative, skip=present)
    tasks, meta = ports.tasks(initiative), ports.meta(initiative)
    if not tasks:
        return Plan(initiative, skip="it has no tasks")
    own: Mapping[str, object] = meta.get("initiative") or {"id": initiative}
    cartridge: Mapping[str, object] = meta.get("cartridge") or {}
    try:
        land_at = resolve_land_at(cartridge, own.get("land_at"))
    except ValueError as exc:
        return Plan(initiative, skip=str(exc))
    if not is_initiative(land_at):
        return Plan(initiative, skip="it lands at task, which keeps no landing record")
    try:
        repo = lander.ports_repo(ports, {"initiative": initiative, "repo": str(own.get("repo") or "")})
    except LookupError as exc:
        return Plan(initiative, skip=str(exc))
    if lg.rev(repo, "HEAD") is None:
        return Plan(initiative, skip=f"its repository is not at {repo}")
    parent = str(meta.get("target_branch") or own.get("target_branch") or _default_branch(repo))
    parent_ref = lander.parent_ref_of(repo, parent)
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


def apply(ports: lander.Ports, found: Plan, send_back: Callable[[str, str, str], str | None], *,
          adopted: bool = True) -> list[str]:
    """Perform a plan. Each step reads what is already so, and a step that fails stops the rest of this initiative.

    `send_back(initiative, task, reason)` moves an approved task whose work cannot be found back to ready; it returns
    why it was refused, or None. `adopted` is stamped on the record: False when the lander makes it in its normal flow
    (`record_phase_initiatives`) rather than for an initiative in flight before the landing record existed."""
    initiative = found.initiative
    if found.skip:
        return found.lines()
    present = present_skip(ls.read_record(ports.conn, initiative))
    if present is not None:
        return [f"{initiative}: skipped: {present}"]
    own = ports.meta(initiative).get("initiative") or {}
    repo = lander.ports_repo(ports, {"initiative": initiative, "repo": str(own.get("repo") or "")})
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
                                   parent_ref=lander.parent_ref_of(repo, found.parent), now=ports.now(), adopted=adopted,
                                   lane_model=lr.lane_model_of(own.get("lane_model")))
    if record is None:
        return [*events, f"{initiative}: stopped: no record was made: {why}"]
    events.append(f"{initiative}: record made on {lander.record_text(record, 'branch')}")
    if found.landed:
        ls.put_record(ports.conn, lr.with_state({**record, "state": "assembled"}, "landed", ports.now()))
        return [*events, f"{initiative}: recorded landed"]
    for phase in found.merge:
        outcome = lander.merge_phase(ports, initiative, phase)
        events += list(outcome.events) or [f"{initiative}: phase {phase}: {outcome.status}: {outcome.detail}"]
        if outcome.status != "done":
            break  # a later phase may need this one; the lander's own passes take it from here
    return events


def present_skip(found: lr.LandingRecord | lr.Unreadable | None) -> str | None:
    """Pure. Why adopt leaves an initiative alone because it has a landing-record row, or None when it has none. A row
    that does not read is present too: adopting over it would write a fresh record over the history of its merged
    phases, so it is left for a person to repair."""
    if found is None:
        return None
    if isinstance(found, lr.Unreadable):
        return f"its landing record cannot be read and is left as it is ({found.why})"
    return "it has a landing record already"


def unrecorded(initiatives: Sequence[str], table: lr.Listing) -> list[str]:
    """Pure. The initiatives with no landing-record row, readable or not, in the order given (`present_skip`)."""
    rows = _rows(table)
    return [i for i in initiatives if present_skip(rows.get(i)) is None]


def left_alone(initiatives: Sequence[str], table: lr.Listing) -> list[str]:
    """Pure. What adopt says of each initiative named that it leaves alone because its row does not read: a person
    who named it is told why nothing was adopted, and that the row was left as it is."""
    rows = _rows(table)
    found = [(i, rows.get(i)) for i in initiatives]
    return [f"{i}: skipped: {present_skip(row)}" for i, row in found if isinstance(row, lr.Unreadable) and present_skip(row)]


def _rows(table: lr.Listing) -> dict[str, lr.LandingRecord | lr.Unreadable]:
    return {**{str(r.get("initiative")): r for r in table.records}, **{u.initiative: u for u in table.unreadable}}


def candidates(ports: lander.Ports, initiatives: Sequence[str]) -> list[str]:
    """The initiatives with no landing record, in the order given (`unrecorded`)."""
    return unrecorded(initiatives, ls.list_records(ports.conn))


# -- the new model's record, made in the lander's own pass ------------------------------------------------------------

RECORD_KEY = "record:"  # the lander_notices key prefix of a phase-model initiative whose record is left to a person
WITHDRAWN = "cancelled"  # a phase-queue row the chair took out of the queue (its phase held, closed or over budget)
RECORD_RETRY_SECONDS = 3600  # a record left alone for a reason the chair was told is tried again this often


def record_now(lane_model: object, phase_rows: Sequence[Mapping[str, object]],
               found: lr.LandingRecord | lr.Unreadable | None) -> bool:
    """Pure. Whether the lander makes an initiative's landing record on its own now. The old model's epic run makes it
    as the run starts; the new model has no run, so the moment is the chair queuing its first phase: the initiative is
    on the phase model, one of its phase-queue rows is a phase (an initiative review is not) the chair has not
    withdrawn, and it has no landing-record row, readable or not (`present_skip`).

    Nothing orders this pass before a worker's claim, so a lane may cut its phase before the record exists. It then
    cuts from the parent (`phase_lane.base_candidates`), where the record's branch is cut from too, and the merge
    is the same: the divergence is a parent that moved in between, which the merge of the phase carries."""
    return (lr.lane_model_of(lane_model) == lr.NEW_MODEL and present_skip(found) is None
            and any(str(r.get("kind") or "") != initiative_review.KIND and str(r.get("state") or "") != WITHDRAWN
                    for r in phase_rows))


def retry_due(since: str, now: str, seconds: int = RECORD_RETRY_SECONDS) -> bool:
    """Pure. Whether an initiative whose record was left alone at `since` is planned again at `now`. Until then its
    branches are not fetched nor its plan made again on every pass; a stamp that does not read is due at once."""
    try:
        return ops.later(since, seconds) <= now
    except ValueError:
        return True


def left_to_a_person(found: Plan) -> str | None:
    """Pure. Why the lander does not adopt this plan on its own, or None when it does. It never sends an approved task
    back to ready, nor carries a commit onto a phase branch, unasked: either means recorded work is not where the new
    model leaves it (origin's phase branch), which a person checks before `cox lander adopt --apply` does it."""
    if found.skip:
        return found.skip
    moved = [f"{f.task} ({'back to ready' if f.action == 'ready' else 'carried onto its phase branch'})"
             for f in found.tasks if f.action in ("ready", "carry")]
    if moved:
        return (f"adopting it would move approved work: {', '.join(moved)}; check it, then run "
                f"`cox lander adopt --apply {found.initiative}`")
    return None


def _never_send_back(initiative: str, task: str, why: str) -> str:
    return "the lander sends no task back on its own"  # `left_to_a_person` keeps every such plan from `apply`


def _planned(ports: lander.Ports, initiative: str, phase_rows: Sequence[Mapping[str, object]]) -> Plan | None:
    """The plan of an initiative that wants a record (`record_now`), against origin's phase branches; None when it
    wants none. Raises what reading the initiative raises."""
    own = ports.meta(initiative).get("initiative") or {}
    if not record_now(own.get("lane_model"), phase_rows, None):
        return None
    found = plan(ports, initiative)
    if found.skip:
        return found
    for line in refresh(ports, initiative):
        ports.log(line)
    return plan(ports, initiative)  # again, now against origin's phase branches


def record_phase_initiatives(ports: lander.Ports) -> list[str]:
    """Edge. Make the landing record of each phase-model initiative that wants one (`record_now`) through adopt's own
    path (`refresh`, `plan`, `apply`), so a record is made one way, stamped `lane_model: phase`, and never twice: an
    initiative with a record is no candidate, and `start_record` inserts only where no row is. No phase is merged
    here: the pass's sweep queues every built phase of the new record and its lane merges it, as for any record. The
    new branch is pushed, so a lane on another machine cuts its phase from it.

    An initiative that cannot be read, that adopt skips, or whose plan would move approved work stops only itself: it
    is left alone, the chair is told once per reason through `lander_notices`, and it is planned again only once
    `retry_due`. The memory is forgotten once the initiative has a record, whoever made it."""
    rows: dict[str, list[store_phases.Row]] = {}
    for row in store_phases.list_rows(ports.conn):
        rows.setdefault(str(row.get("initiative") or ""), []).append(row)
    told = ls.told_since(ports.conn, RECORD_KEY)
    table = ls.list_records(ports.conn)
    wanting = unrecorded(sorted(i for i in rows if i), table)
    events: list[str] = []
    for key in told:
        if key.removeprefix(RECORD_KEY) not in wanting:
            ls.forget(ports.conn, key)  # recorded since (an operator's adopt), or no longer queued
    for initiative in wanting:
        key = f"{RECORD_KEY}{initiative}"
        reason, since = told.get(key, ("", ""))
        if since and not retry_due(since, ports.now()):
            continue
        try:
            found = _planned(ports, initiative, rows[initiative])
        except Exception as exc:  # one initiative whose files or tasks cannot be read: only it waits
            found, why = None, f"it cannot be read: {type(exc).__name__}: {exc}"
        else:
            why = None if found is None else left_to_a_person(found)
        if why is not None:
            ls.remember(ports.conn, key, why, ports.now())
            if why != reason:
                ports.notify("needs_chair", {"initiative": initiative, "reason": why})
                events.append(f"{initiative}: no landing record is made on its own: {why}")
            continue
        if found is None:
            continue
        events += apply(ports, replace(found, merge=()), _never_send_back, adopted=False)
        record = ls.get_record(ports.conn, initiative)
        if record is not None and not found.landed:
            pushed = publish_initiative(ports, lander.ports_repo(ports, record), lander.record_text(record, "branch"))
            events += [f"{initiative}: {pushed}; a lane cuts its phase from the parent until origin has it"] if pushed else []
        if key in told:
            ls.forget(ports.conn, key)
    return events
