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
only when the doctor's lane checks pass.

Order is `order`'s (C5), and a worker claims by it (`phase_pick.choices`, lowest `(band, seq)` first): band 0 for an
initiative whose intake `kind` is a hotfix or a bug, else band 1; inside a band, explicit priority first (the docket's,
which `cox route priority` sets), then first in first out by the first time any phase of the initiative was queued
(its rows' `queued_at`, the store's clock); the initiatives of a band that share a `target_release` placed together at
the first one's position; and every ready phase its own row, so nothing waits behind another repository's work. Each
pass places every row a worker could claim (`placements`): the wanted rows, and the stored waiting, ready and `built`
rows the docket no longer lists, around the places claimed rows hold, so no two rows share a `(band, seq)`.

A claimed row is the worker's and is never changed here, but for one mark. A claim whose lease lapsed with nobody
taking it over is a lost run (its worker died, or lost the store): the chair writes the "run lost" mark as the row's
reason (`mark_lost`, `phase_pick.lost_reason`) and tells the operator (one `needs_chair`, cause `RUN_LOST_CAUSE`) only
when that write changed the row (`lost_runs`). The tick plans on its own clock, but the mark lands only on a claim
lapsed by the store's, at the epoch the tick saw, and only once; so a claim that is still live (the chair's clock runs
ahead) or was taken over meanwhile is neither marked nor told about. Nothing else is done for it: the lapsed claim is claimable, the next
worker that claims it starts where the branch stands (`lane_progress.resume_plan`), and a phase whose claims ran out
is exhausted as before. A lost run taken over before any tick saw it lapse gets no mark: it was recovered. A withheld phase's ready row is withdrawn (`cancelled`) and
comes back when it is wanted again. A finished row is reopened when its phase is ready again and it finished approved
or quarantined: work came back to it, and the reopen starts a new review episode (`store_phases`). A row whose claims
ran out while nobody held it is finished as failed (`exhaust`), and so is any phase that ran out of attempts: it stays
finished for the operator. A row an older worker parked `built` (L1's interim, before the lane reviewed its own phase)
is made claimable again, so its next lane reviews it; the requeue counts an attempt, since that worker's park gave
its claim's back, so an old worker that keeps parking the row runs it out of attempts rather than looping.

The phase lane reviews its own phase (L6, `harness.review_loop`), and the chair reacts to how the review finished it:
- approved: each task is approved at its roll-up commit (`approvals`, through `store_api.approve_on`);
- quarantined: each of its open tasks is quarantined with the review's reason (`quarantines`), marked with the cause
  triage reads (`chair_phase_quarantine.MARK_KEY`), so the work goes to triage rather than straight back to a lane.
Neither phase is wanted again while its tasks wait for that write.

A new-model initiative assembled on its initiative branch is reviewed as a whole before its pull request
(`chair_initiative_review`, D2): its one `initiative_review` row, the revision phase a `revise` round asks for, and the
same review queued again once that phase has merged.

An initiative on the phase model is the worker's for its whole life: `withhold_old_model` drops every action of the
old model (a launch, a relaunch, a branch clear, a fetch, a land, a signal to a run) that names one, so the two models
never build, wipe or land the same phase.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from itertools import count
from typing import cast

from agent_tools import chair_initiative_review
from agent_tools.chair_phase_quarantine import MARK_KEY
from agent_tools.chair_types import Action, ActionKind, Facts, InitiativeFacts
from agent_tools.lane_prereqs import DOCKER_CAPABILITY
from agent_tools.queue_rows import launchable_by_tick
from core.intake import URGENT_KINDS
from harness import initiative_review, phase_pick, review_loop

BAND_URGENT = 0  # an initiative whose intake was filed as a hotfix or a bug
BAND = 1  # every other phase
_SEQ_STRIDE = 1000  # phases per initiative the docket order (`rows`, before `order`) leaves room for
_UNSEEN = "~"  # sorts after every stamp: an initiative with no first-queued time goes last in its band
_REOPENED = frozenset({"approved", "quarantined"})
RUN_LOST_CAUSE = "run_lost"  # the needs_chair cause that tells the operator a claim's run was lost
STATUS_COMMAND = ("cox", "worker", "status")  # where the operator sees the lapsed claim and who could take it

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


def band_of(kind: str) -> int:
    """Pure. The band of an initiative whose intake `kind` is `kind`: 0 for a hotfix or a bug, else 1."""
    return BAND_URGENT if kind.strip().lower() in URGENT_KINDS else BAND


def order(phases: Sequence[Key], releases: Mapping[str, str], kinds: Mapping[str, str],
          first_seen: Mapping[str, str], priorities: Mapping[str, int] | None = None,
          taken: Collection[tuple[int, int]] = ()) -> dict[Key, tuple[int, int]]:
    """Pure. The `(band, seq)` each ready phase is claimed by, lowest first (`phase_pick.choices`).

    `phases` are the ready phases in docket order, each initiative's in its own phase order; `releases` maps an
    initiative to its `target_release` ("" or absent: none), `kinds` to its intake's `kind`, `first_seen` to the
    store's stamp of the first time a phase of it was queued, and `priorities` to its explicit priority (absent: 0).

    1. band 0 for an initiative whose intake kind is a hotfix or a bug, else 1;
    2. inside a band, explicit priority first, higher first (`cox route priority`, the docket's), so an operator's
       `--top` still reorders phase work; then first in first out by `first_seen`; then docket place, then id, so a
       tie is stable;
    3. the initiatives of one band that share a `target_release` follow one another from the first one's position in
       that order, so a release's work is claimed together; an urgent initiative is never moved into band 1 to join
       its release. A release whose first member was queued long ago pulls its newly filed members forward with it;
    4. each phase is its own row with its own seq: nothing groups or serialises by repository, so a worker that meets
       only a later row's requirements takes that row at once (`phase_pick.choices` pages past what it cannot meet).
    Seqs count from 0 inside each band, one per phase, in claim order, skipping each `(band, seq)` in `taken`: the
    places of rows this pass cannot rewrite (claimed ones), so no two rows ever share a place."""
    place = {i: n for n, i in enumerate(dict.fromkeys(i for i, _ in phases))}
    own: dict[str, list[Key]] = {}
    for key in phases:
        own.setdefault(key[0], []).append(key)
    rank = priorities or {}

    def release(initiative: str) -> str:
        return (releases.get(initiative) or "").strip()

    def line(initiative: str) -> tuple[int, str, int, str]:
        return (-rank.get(initiative, 0), first_seen.get(initiative) or _UNSEEN, place[initiative], initiative)

    out: dict[Key, tuple[int, int]] = {}
    for band in (BAND_URGENT, BAND):
        members = sorted((i for i in place if band_of(kinds.get(i, "")) == band), key=line)
        arranged: list[str] = []
        for initiative in members:
            if initiative not in arranged:
                mates = [m for m in members if release(m) == release(initiative)] if release(initiative) else [initiative]
                arranged.extend(m for m in mates if m not in arranged)
        free = (seq for seq in count() if (band, seq) not in taken)
        out.update({key: (band, next(free)) for initiative in arranged for key in own[initiative]})
    return out


def first_queued(stored: Sequence[Row]) -> dict[str, str]:
    """Pure. Each initiative's earliest `queued_at` over its stored rows, finished ones too: the first time any phase
    of it was ready. A reopen keeps a row's `queued_at`, so work that comes back keeps its place in the line."""
    out: dict[str, str] = {}
    for row in stored:
        initiative, at = str(row.get("initiative")), str(row.get("queued_at") or "")
        if at and (initiative not in out or at < out[initiative]):
            out[initiative] = at
    return out


_RENUMBERED = frozenset({"waiting", "ready", "built"})  # stored states whose place a pass writes (update, review)


def _place_of(row: Row) -> tuple[int, int] | None:
    band, seq = row.get("band"), row.get("seq")
    return (band, seq) if isinstance(band, int) and isinstance(seq, int) else None


def placements(wanted: Sequence[Row], stored: Sequence[Row], releases: Mapping[str, str], kinds: Mapping[str, str],
               now: str, priorities: Mapping[str, int] | None = None,
               leaving: Collection[Key] = ()) -> dict[Key, tuple[int, int]]:
    """Pure. One order pass over every row a worker could claim, so no two of them share a `(band, seq)`.

    Placed by `order`: each `wanted` row, and each stored waiting, ready or `built` row nobody wants this pass (a
    `built` row is made ready by its `review`, and a row the docket no longer lists keeps its place in the queue)
    unless it is `leaving` (withdrawn or exhausted this pass). A stored row placed this way goes after the wanted
    ones of its initiative. A claimed row is the worker's and keeps its place: that place is `taken`, so the pass
    numbers round it, and a wanted phase whose row is claimed keeps its own. An initiative with no stored row yet is
    first queued `now`, the tick's clock, so a new one goes behind every one already waiting at its priority."""
    by_key = {_key(r): r for r in stored}
    claimed = {_key(r): p for r in stored if r.get("state") == "claimed" and (p := _place_of(r)) is not None}
    wanted_keys = [_key(w) for w in wanted]
    skip = {*wanted_keys, *leaving}
    extra = sorted((r for r in stored if r.get("state") in _RENUMBERED and _key(r) not in skip),
                   key=lambda r: (*(_place_of(r) or (BAND, 0)), _key(r)))
    keys = [k for k in [*wanted_keys, *map(_key, extra)] if k not in claimed and by_key.get(k, {}).get("state") != "claimed"]
    seen = {**{k[0]: now for k in keys}, **first_queued(stored)}
    return {**order(keys, releases, kinds, seen, priorities, set(claimed.values())), **claimed}


def ordered(wanted: Sequence[Row], placed: Mapping[Key, tuple[int, int]]) -> list[dict[str, object]]:
    """Pure. `wanted` with each row's `band` and `seq` from `placements`; a row it did not place keeps its own."""
    out: list[dict[str, object]] = []
    for w in wanted:
        band, seq = placed.get(_key(w), (w.get("band"), w.get("seq")))
        out.append({**w, "band": band, "seq": seq})
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
            held_phases: Collection[Key] = (), closed: Collection[str] = (), stopped: bool = False,
            placed: Mapping[Key, tuple[int, int]] | None = None) -> list[dict[str, object]]:
    """What the store needs so it holds `wanted_rows`, at the tick's `now`:
    - `review` for a `built` row (parked by an older worker, before the lane reviewed its own phase): claimable again,
      at the cost of an attempt (the park gave its claim's back), so its next lane reviews it and an old worker that
      parks it again and again exhausts it; it carries its place from `placed`, when the pass placed it;
    - `insert` for a phase with no row; `update` for a waiting or ready row whose requirements or order moved, and for
      one nobody wants this pass whose place in `placed` moved (its own requirements and repository kept);
    - `reopen` for a finished (approved or quarantined) or withdrawn row whose phase is wanted again;
    - `withdraw` for a waiting or ready row whose phase is withheld (`withheld`);
    - `exhaust` for a row whose claims ran out while nobody holds it (`phase_pick.exhausted`).
    The store re-checks each against the state it moves from, so a row a worker claimed meanwhile is left alone."""
    by_key = {_key(r): r for r in stored}
    wanted_keys = {_key(w) for w in wanted_rows}
    where = placed or {}
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
        at = where.get(key)
        if phase_pick.exhausted(row, now):
            out.append({"op": "exhaust", "initiative": key[0], "phase": key[1],
                        "reason": f"{row.get('attempts')} claims and none finished it; the operator decides"})
        elif row.get("state") == "built":
            out.append({"op": "review", "initiative": key[0], "phase": key[1], "epoch": row.get("claim_epoch", 0),
                        **({"band": at[0], "seq": at[1]} if at is not None else {}),
                        "reason": "parked built before the lane reviewed its own phase: its next lane reviews it (one attempt)"})
        elif row.get("state") in ("waiting", "ready") and (why := withheld(key, holds, held_phases, closed, stopped)):
            out.append({"op": "withdraw", "initiative": key[0], "phase": key[1], "reason": why})
        elif row.get("state") in ("waiting", "ready") and key not in wanted_keys and at is not None and at != _place_of(row):
            out.append({"op": "update", "initiative": key[0], "phase": key[1], "kind": str(row.get("kind") or "build"),
                        "repo": str(row.get("repo") or ""), "requires": _requires_of(row), "band": at[0], "seq": at[1]})
    return out


def lost_runs(stored: Sequence[Row], now: str) -> list[Action]:
    """The lost runs as of `now`, the tick's own clock: each claimed row whose lease lapsed (`phase_pick.lapsed`), whose
    claims have not run out (that one is exhausted instead), and whose reason is not the mark yet. For each, a
    `queue_phases` of its one `mark_lost` change carrying the needs_chair `notice`, which the chair records only when
    the mark changed the row (`chair_exec_act`): by the store's clock, at the epoch seen here, once."""
    out: list[Action] = []
    for row in stored:
        if not phase_pick.lapsed(row, now) or phase_pick.exhausted(row, now):
            continue
        raw = row.get("claim_epoch")
        (initiative, phase), epoch, why = _key(row), raw if isinstance(raw, int) else 0, phase_pick.lost_reason(row)
        if str(row.get("reason") or "") == why:
            continue
        notice: Action = {"kind": ActionKind.NEEDS_CHAIR, "initiative": initiative, "phase": phase,
                          "cause": RUN_LOST_CAUSE, "claim_epoch": epoch, "worker": str(row.get("claimed_by") or ""),
                          "run": str(row.get("run_id") or ""), "reason": why, "command": list(STATUS_COMMAND)}
        out.append({"kind": ActionKind.QUEUE_PHASES, "initiative": initiative, "phase": phase,
                    "changes": [{"op": "mark_lost", "initiative": initiative, "phase": phase, "epoch": epoch,
                                 "reason": why}],
                    "notice": notice, "reason": f"phase queue: {initiative}/{phase} mark_lost"})
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


_OPEN = frozenset({"todo", "ready", "in_progress"})  # the task states a phase's quarantine moves


def quarantines(stored: Sequence[Row], tasks: Sequence[Row]) -> list[dict[str, object]]:
    """The `queue_phases` changes that quarantine each open task (todo, ready or in progress) of a phase whose row
    finished quarantined: its review quarantined it, and the row's reason is the round's. Each moves from the state
    read, and marks the task with the review's cause, that reason and the run that reviewed it, which triage reads. A
    task approved, done, dropped, blocked or quarantined already is left as it is, so a repeat plans nothing; and a
    phase finished quarantined because its tasks had stopped (`phase_pick.verdict`) has no open task to move."""
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
    """One `approve_phase_tasks` action for the tasks of phases that finished approved (`approvals`), one
    `queue_phases` action with the tick's changes and the task quarantines of phases that finished quarantined
    (`quarantines`), and one `queue_phases` per lost run, its mark with its notice (`lost_runs`); each is left out
    when it has nothing. Without the stored rows (`phase_queue` unread) nothing is
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
    withheld_ids = {i for i in queue["phase_initiatives"]
                    if withheld((i, initiative_review.QUEUE_PHASE), held, held_phases, closed, stopped)}
    reviews = chair_initiative_review.review_changes(
        queue.get("records") or [], queue.get("tasks") or [], queue["rows"], queue.get("initiative_rounds") or {},
        lambda repo: requirements([], repo.rstrip("/").rpartition("/")[2], queue["lane_prereqs"]), withheld_ids)
    leaving = {_key(r) for r in queue["rows"] if phase_pick.exhausted(r, now) or (
        r.get("state") in ("waiting", "ready") and withheld(_key(r), held, held_phases, closed, stopped))}
    placed = placements(wanted, queue["rows"], queue.get("releases") or {}, queue.get("kinds") or {}, now,
                        queue.get("priorities") or {}, leaving)
    wanted = ordered(wanted, placed)
    found = [*changes(wanted, queue["rows"], now, held, held_phases, closed, stopped, placed), *quarantine, *reviews]
    out: list[Action] = []
    if approve:
        names = ", ".join(f"{a['initiative']}/{a['task_id']}@{a['commit'][:12]}" for a in approve)
        out.append({"kind": ActionKind.APPROVE_PHASE_TASKS, "approvals": approve,
                    "reason": f"the phase review approved: {names}"})
    if found:
        names = ", ".join(f"{c['initiative']}/{c['phase']}{'/' + str(c['task_id']) if 'task_id' in c else ''} {c['op']}" for c in found)
        out.append({"kind": ActionKind.QUEUE_PHASES, "changes": found, "reason": f"phase queue: {names}"})
    return [*out, *lost_runs(queue["rows"], now)]
