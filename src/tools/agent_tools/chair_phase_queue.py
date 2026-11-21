"""The chair's side of the phase queue, pure: one `phase_queue` row per ready phase of a `lane_model: phase` initiative,
and the one rule that keeps every old-model action off those initiatives.

The tick reads the stored rows (`Facts.phase_queue`), works out the rows it wants from the initiatives it already
gathered (`rows`), and plans one `queue_phases` action carrying only the differences (`changes`). Nothing is planned
when the store already holds what the chair wants, so a repeat tick plans nothing and a repeat write changes nothing.

A phase is wanted when it can start a lane now (`InitiativeFacts.phases`, the rule the tick launches by) and nothing
withholds it: its initiative held, deferred or stopped by its own failures, one of its tasks held for a person, the
initiative closed, or the hard stop on spend. Its requirements are the union of its ready tasks' `requires:`,
`repo:<name>` for the initiative's repository, and `docker` while the lane-prerequisite gate is on
(`policy.dispatch.lane_prereqs`, on by default), since every phase runs the smoke suite; a worker advertises `docker`
only when the doctor's lane checks pass. Order is the docket's: `band` 1 today, `seq` the initiative's place in the
docket, then the phase's place in it.

A claimed row is the worker's and is never changed here. A withheld phase's ready row is withdrawn (`cancelled`) and
comes back when it is wanted again. A finished row is reopened when its phase is ready again and it finished approved
or quarantined: work came back to it, and the reopen starts a new review episode (`store_phases`). A row whose claims
ran out while nobody held it is finished as failed (`exhaust`), and so is any phase that ran out of attempts: it stays
finished for the operator. A row an older worker parked `built` (L1's interim, before the lane reviewed its own phase)
is made claimable again, so its next lane reviews it.

The phase lane reviews its own phase (L6, `harness.review_loop`), and the chair reacts to how the review finished it:
- approved: each task is approved at its roll-up commit (`approvals`, through `store_api.approve_on`);
- quarantined: each of its open tasks is quarantined with the review's reason (`quarantines`), marked with the cause
  triage reads (`chair_phase_quarantine.MARK_KEY`), so the work goes to triage rather than straight back to a lane.
Neither phase is wanted again while its tasks wait for that write.

An initiative on the phase model is the worker's for its whole life: `withhold_old_model` drops every action of the
old model (a launch, a relaunch, a branch clear, a fetch, a land, a signal to a run) that names one, so the two models
never build, wipe or land the same phase.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import cast

from agent_tools.chair_phase_quarantine import MARK_KEY
from agent_tools.chair_types import Action, ActionKind, Facts, InitiativeFacts
from agent_tools.lane_prereqs import DOCKER_CAPABILITY
from agent_tools.queue_rows import launchable_by_tick
from harness import phase_pick, review_loop

BAND = 1  # every phase today; band 0 is kept for hotfixes and bugs
_SEQ_STRIDE = 1000  # phases per initiative the order leaves room for
_REOPENED = frozenset({"approved", "quarantined"})

# The old model's actions on an initiative: each starts, signals, fetches, rewrites or lands a run or its branches.
# None may touch a phase initiative, whose phases a worker builds on branches it owns.
OLD_MODEL_KINDS = frozenset({
    ActionKind.LAUNCH_EPIC, ActionKind.RELAUNCH, ActionKind.RETRY, ActionKind.RESCUE, ActionKind.CLEAR_BRANCHES,
    ActionKind.REBUILD_QUARANTINED, ActionKind.FETCH, ActionKind.FETCH_EXIT, ActionKind.CARRY_PHASE,
    ActionKind.REBASE_PHASE, ActionKind.STALLED_USR1, ActionKind.STALLED_STOP, ActionKind.MARK_LOST, ActionKind.LAND,
    ActionKind.LAND_PHASE, ActionKind.PARTIAL_LAND,
})

type Row = Mapping[str, object]
type Key = tuple[str, str]


def phase_model_ids(facts: Facts) -> frozenset[str]:
    """Every initiative on the phase model the tick knows of: stamped so on its docket entry, or on its store row
    (`Facts.phase_queue.phase_initiatives`, which covers initiatives with no ready task in the docket)."""
    queue = facts.get("phase_queue")
    stored = frozenset(queue["phase_initiatives"]) if queue is not None else frozenset[str]()
    return stored | frozenset(i["id"] for i in facts.get("initiatives") or [] if not launchable_by_tick(i))


def withhold_old_model(actions: Sequence[Action], phase_ids: Collection[str]) -> list[Action]:
    """`actions` without the old model's actions on a phase initiative (`OLD_MODEL_KINDS`)."""
    return [a for a in actions if not (a.get("kind") in OLD_MODEL_KINDS and a.get("initiative", "") in phase_ids)]


def _repo_dir(initiative: InitiativeFacts) -> str:
    return initiative.get("repo_dir") or str(initiative.get("repo") or "").rstrip("/").rpartition("/")[2]


def requirements(task_requires: Sequence[str], repo_dir: str, lane_prereqs: bool) -> list[str]:
    """The phase's requirement tokens, sorted: its tasks', its repository's, and `docker` while the gate is on."""
    return sorted({
        *task_requires, *([f"repo:{repo_dir}"] if repo_dir else []), *([DOCKER_CAPABILITY] if lane_prereqs else []),
    })


def withheld(key: Key, holds: Collection[str], held_phases: Collection[Key], closed: Collection[str], stopped: bool) -> str:
    """Why the phase `key` must not be queued now, or "" when nothing withholds it."""
    initiative, _ = key
    if initiative in closed:
        return "its initiative is closed"
    if initiative in holds:
        return "its initiative is held"
    if key in held_phases:
        return "a task of the phase is held for a person"
    return "the hard stop on spend holds every phase" if stopped else ""


def rows(initiatives: Sequence[InitiativeFacts], lane_prereqs: bool, holds: Collection[str] = (),
         held_phases: Collection[Key] = (), closed: Collection[str] = (), stopped: bool = False) -> list[dict[str, object]]:
    """The row the chair wants for each ready phase of each phase initiative that nothing withholds, in docket order."""
    out: list[dict[str, object]] = []
    for place, initiative in enumerate(i for i in initiatives if not launchable_by_tick(i)):
        for index, phase in enumerate(initiative.get("phases") or []):
            if withheld((initiative["id"], phase["phase"]), holds, held_phases, closed, stopped):
                continue
            requires = [r for t in phase["ready_tasks"] for r in t["requires"]]
            out.append({
                "initiative": initiative["id"], "phase": phase["phase"], "kind": "build",
                "repo": str(initiative.get("repo") or ""), "requires": requirements(requires, _repo_dir(initiative), lane_prereqs),
                "band": BAND, "seq": place * _SEQ_STRIDE + index,
            })
    return out


def _requires_of(row: Row) -> list[str]:
    raw = row.get("requires_json")
    return sorted(str(t) for t in cast("list[object]", raw)) if isinstance(raw, list) else []


def _same(want: Row, stored: Row) -> bool:
    return (
        _requires_of(stored) == want["requires"] and stored.get("band") == want["band"]
        and stored.get("seq") == want["seq"] and str(stored.get("repo") or "") == want["repo"]
    )


def _key(row: Row) -> Key:
    return (str(row.get("initiative")), str(row.get("phase")))


def changes(wanted_rows: Sequence[Row], stored: Sequence[Row], now: str, holds: Collection[str] = (),
            held_phases: Collection[Key] = (), closed: Collection[str] = (), stopped: bool = False) -> list[dict[str, object]]:
    """What the store needs so it holds `wanted_rows`, at the tick's `now`:
    - `review` for a `built` row (parked by an older worker, before the lane reviewed its own phase): claimable again,
      its attempts as they were, so its next lane reviews it;
    - `insert` for a phase with no row; `update` for a waiting or ready row whose requirements or order moved;
    - `reopen` for a finished (approved or quarantined) or withdrawn row whose phase is wanted again;
    - `withdraw` for a waiting or ready row whose phase is withheld (`withheld`);
    - `exhaust` for a row whose claims ran out while nobody holds it (`phase_pick.exhausted`).
    The store re-checks each against the state it moves from, so a row a worker claimed meanwhile is left alone."""
    by_key = {_key(r): r for r in stored}
    out: list[dict[str, object]] = []
    for want in wanted_rows:
        found = by_key.get(_key(want))
        state = None if found is None else found.get("state")
        if found is None:
            out.append({"op": "insert", **want, "reason": "ready to build"})
        elif phase_pick.exhausted(found, now):
            continue  # the pass over stored rows below finishes it
        elif state in ("waiting", "ready") and not _same(want, found):
            out.append({"op": "update", **want})
        elif state == "cancelled" or (state == "finished" and found.get("outcome") in _REOPENED):
            was = "withdrawn" if state == "cancelled" else f"finished {found.get('outcome')}"
            out.append({"op": "reopen", **want, "epoch": found.get("claim_epoch", 0), "reason": f"ready again after it was {was}"})
    for row in stored:
        key = _key(row)
        if phase_pick.exhausted(row, now):
            out.append({"op": "exhaust", "initiative": key[0], "phase": key[1],
                        "reason": f"{row.get('attempts')} claims and none finished it; the operator decides"})
        elif row.get("state") == "built":
            out.append({"op": "review", "initiative": key[0], "phase": key[1], "epoch": row.get("claim_epoch", 0),
                        "reason": "parked built before the lane reviewed its own phase: its next lane reviews it"})
        elif row.get("state") in ("waiting", "ready") and (why := withheld(key, holds, held_phases, closed, stopped)):
            out.append({"op": "withdraw", "initiative": key[0], "phase": key[1], "reason": why})
    return out


_DONE = frozenset({"done", "dropped"})


def approvals(stored: Sequence[Row], tasks: Sequence[Row], rollups: Sequence[Row]) -> list[dict[str, str]]:
    """The tasks to approve: each task of a phase whose row finished approved that is not approved at its last
    roll-up commit yet (not approved, or approved with another or no `merged_commit`), and not done or dropped, with
    that commit. Commits are keyed by initiative, phase and task, so a task id two phases share never takes the
    other's. A task with no roll-up commit is left alone: there is nothing on the branch to record."""
    approved = {_key(r) for r in stored if r.get("state") == "finished" and r.get("outcome") == "approved"}
    commits = {(str(r.get("initiative")), str(r.get("phase")), str(r.get("task_id"))): str(r.get("subject_sha"))
               for r in rollups}
    out: list[dict[str, str]] = []
    for task in sorted(tasks, key=lambda t: (str(t.get("initiative")), str(t.get("phase")), str(t.get("task_id")))):
        initiative, phase, task_id = str(task.get("initiative")), str(task.get("phase")), str(task.get("task_id"))
        commit = commits.get((initiative, phase, task_id), "")
        state, merged = task.get("state"), str(task.get("merged_commit") or "")
        settled = state in _DONE or (state == "approved" and merged == commit)
        if (initiative, phase) in approved and commit and not settled:
            out.append({"initiative": initiative, "phase": phase, "task_id": task_id, "commit": commit})
    return out


_OPEN = frozenset({"todo", "ready", "in_progress", "blocked"})  # the task states a phase's quarantine moves


def quarantines(stored: Sequence[Row], tasks: Sequence[Row]) -> list[dict[str, object]]:
    """The `queue_phases` changes that quarantine each open task (todo, ready, in progress or blocked) of a phase whose
    row finished quarantined: its review quarantined it, and the row's reason is the round's. Each moves from the state
    read, and marks the task with the review's cause, that reason and the run that reviewed it, which triage reads. A
    task approved, done, dropped or quarantined already is left as it is, so a repeat plans nothing."""
    finished = {_key(r): r for r in stored if r.get("state") == "finished" and r.get("outcome") == "quarantined"}
    out: list[dict[str, object]] = []
    for task in sorted(tasks, key=lambda t: (str(t.get("initiative")), str(t.get("phase")), str(t.get("task_id")))):
        row = finished.get((str(task.get("initiative")), str(task.get("phase"))))
        if row is None or task.get("state") not in _OPEN:
            continue
        mark = {"cause": review_loop.REVIEW_CAUSE, "reason": str(row.get("reason") or ""), "run": str(row.get("run_id") or "")}
        out.append({"op": review_loop.QUARANTINE_TASK, "initiative": str(task.get("initiative")),
                    "phase": str(task.get("phase")), "task_id": str(task.get("task_id")), "from": str(task.get("state")),
                    "mark_key": MARK_KEY, "mark": mark})
    return out


def plan(facts: Facts, now: str, holds: Collection[str] = ()) -> list[Action]:
    """One `approve_phase_tasks` action for the tasks of phases that finished approved (`approvals`), and one
    `queue_phases` action with the tick's changes and the task quarantines of phases that finished quarantined
    (`quarantines`); either is left out when it has nothing. Without the stored rows (`phase_queue` unread) nothing is
    planned. A phase whose tasks wait to be approved or quarantined is not wanted again: its ready tasks are what this
    tick settles, not work that came back. `holds` is the tick's held and deferred initiatives; the facts add those
    stopped by their own failures and the tasks held for a person."""
    queue = facts.get("phase_queue")
    if queue is None or not queue["readable"]:
        return []
    held = frozenset(holds) | frozenset(facts.get("held_initiatives") or []) | frozenset(facts.get("no_progress") or {})
    held_phases = frozenset((t["initiative"], t["phase"]) for t in facts.get("held_tasks") or [])
    closed, stopped = frozenset(queue["closed"]), bool(facts["limits"]["hard_stop"])
    approve = approvals(queue["rows"], queue.get("tasks") or [], queue.get("rollups") or [])
    quarantine = quarantines(queue["rows"], queue.get("tasks") or [])
    pending = {(a["initiative"], a["phase"]) for a in approve} | {_key(q) for q in quarantine}
    wanted = [w for w in rows(facts.get("initiatives") or [], queue["lane_prereqs"], held, held_phases, closed, stopped)
              if _key(w) not in pending]
    found = [*changes(wanted, queue["rows"], now, held, held_phases, closed, stopped), *quarantine]
    out: list[Action] = []
    if approve:
        names = ", ".join(f"{a['initiative']}/{a['task_id']}@{a['commit'][:12]}" for a in approve)
        out.append({"kind": ActionKind.APPROVE_PHASE_TASKS, "approvals": approve,
                    "reason": f"the phase review approved: {names}"})
    if found:
        names = ", ".join(f"{c['initiative']}/{c['phase']}{'/' + str(c['task_id']) if 'task_id' in c else ''} {c['op']}" for c in found)
        out.append({"kind": ActionKind.QUEUE_PHASES, "changes": found, "reason": f"phase queue: {names}"})
    return out
