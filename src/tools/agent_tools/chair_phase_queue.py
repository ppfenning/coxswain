"""The chair's side of the phase queue, pure: one `phase_queue` row per ready phase of a `lane_model: phase` initiative.

The tick reads the stored rows (`Facts.phase_queue`), computes the rows it wants from the initiatives it already
gathered, and plans one `queue_phases` action carrying only the differences (`changes`). Nothing is planned when the
store already holds what the chair wants, so a repeat tick plans nothing and a repeat write changes nothing.

A phase is ready when it can start a lane now (`InitiativeFacts.phases`, the rule the tick launches by). Its
requirements are the union of its ready tasks' `requires:`, `repo:<name>` for the initiative's repository, and
`docker` while the lane-prerequisite gate is on (`policy.dispatch.lane_prereqs`, on by default), since every phase
runs the smoke suite; a worker advertises `docker` only when the doctor's lane checks pass. Order is the docket's:
`band` 1 for every phase today, `seq` the initiative's place in the docket, then the phase's place in it.

A claimed row is the worker's and is never changed here. A finished row is reopened only when its phase is ready
again and it finished approved or quarantined: work came back to it (a revision, a rebuilt quarantine). A phase that
ran out of attempts (`failed`) stays finished for the operator.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from agent_tools.chair_types import Action, ActionKind, Facts, InitiativeFacts
from agent_tools.lane_prereqs import DOCKER_CAPABILITY
from agent_tools.queue_rows import launchable_by_tick

BAND = 1  # every phase today; band 0 is kept for hotfixes and bugs
_SEQ_STRIDE = 1000  # phases per initiative the order leaves room for
_REOPENED = frozenset({"approved", "quarantined"})

type Row = Mapping[str, object]


def _repo_dir(initiative: InitiativeFacts) -> str:
    return initiative.get("repo_dir") or str(initiative.get("repo") or "").rstrip("/").rpartition("/")[2]


def requirements(task_requires: Sequence[str], repo_dir: str, lane_prereqs: bool) -> list[str]:
    """The phase's requirement tokens, sorted: its tasks', its repository's, and `docker` while the gate is on."""
    return sorted({
        *task_requires, *([f"repo:{repo_dir}"] if repo_dir else []), *([DOCKER_CAPABILITY] if lane_prereqs else []),
    })


def wanted(initiatives: Sequence[InitiativeFacts], lane_prereqs: bool) -> list[dict[str, object]]:
    """The row the chair wants for each ready phase of each phase initiative, in docket order."""
    rows: list[dict[str, object]] = []
    for place, initiative in enumerate(i for i in initiatives if not launchable_by_tick(i)):
        for index, phase in enumerate(initiative.get("phases") or []):
            requires = [r for t in phase["ready_tasks"] for r in t["requires"]]
            rows.append({
                "initiative": initiative["id"], "phase": phase["phase"], "kind": "build",
                "repo": str(initiative.get("repo") or ""), "requires": requirements(requires, _repo_dir(initiative), lane_prereqs),
                "band": BAND, "seq": place * _SEQ_STRIDE + index,
            })
    return rows


def _requires_of(row: Row) -> list[str]:
    raw = row.get("requires_json")
    return sorted(str(t) for t in cast("list[object]", raw)) if isinstance(raw, list) else []


def _same(want: Row, stored: Row) -> bool:
    return (
        _requires_of(stored) == want["requires"] and stored.get("band") == want["band"]
        and stored.get("seq") == want["seq"] and str(stored.get("repo") or "") == want["repo"]
    )


def changes(wanted_rows: Sequence[Row], stored: Sequence[Row]) -> list[dict[str, object]]:
    """What the store needs so it holds `wanted_rows`: an `insert` for a phase with no row, an `update` for a waiting
    or ready row whose requirements or order moved, and a `reopen` for a finished row whose phase is ready again."""
    by_key = {(str(r.get("initiative")), str(r.get("phase"))): r for r in stored}
    out: list[dict[str, object]] = []
    for want in wanted_rows:
        found = by_key.get((str(want["initiative"]), str(want["phase"])))
        state = None if found is None else found.get("state")
        if found is None:
            out.append({"op": "insert", **want, "reason": "ready to build"})
        elif state in ("waiting", "ready") and not _same(want, found):
            out.append({"op": "update", **want})
        elif state == "finished" and found.get("outcome") in _REOPENED:
            out.append({"op": "reopen", **want, "epoch": found.get("claim_epoch", 0),
                        "reason": f"ready again after it finished {found.get('outcome')}"})
    return out


def plan(facts: Facts) -> list[Action]:
    """One `queue_phases` action with the tick's changes, or nothing: no phase initiative is ready, or the store
    already holds every row. Without the stored rows (`phase_queue` unread) nothing is planned."""
    queue = facts.get("phase_queue")
    if queue is None:
        return []
    found = changes(wanted(facts.get("initiatives") or [], queue["lane_prereqs"]), queue["rows"])
    if not found:
        return []
    names = ", ".join(f"{c['initiative']}/{c['phase']} {c['op']}" for c in found)
    return [{"kind": ActionKind.QUEUE_PHASES, "changes": found, "reason": f"phase queue: {names}"}]
