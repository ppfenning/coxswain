"""C3, C4: at any moment exactly one of the tick and a kind's queue worker plans that kind, and the two together plan
what the tick alone did. The movable kinds are housekeeping, watch, quarantine and decompose, and every subset of them
is checked.

Every scenario of the C1 split (`test_chair_plan_kinds._SCENARIOS`) is planned three ways from the same facts: by the
tick with no queues, by the tick with each subset of the movable kinds queued (or held by a live worker lease), and by
the queue worker of each kind. No double, no gap: the tick's actions plus every acting worker's are the tick's actions
with no queues, action for action. Nothing is stubbed; these are the planners the tick and the worker run.
"""

import itertools
import json
from collections import Counter

import pytest

from agent_tools import chair_kinds
from agent_tools.chair_plan import PlanMismatch, launch_slot_kind, plan_kind, plan_tick, tag_queue_kinds
from agent_tools.chair_queue import planned_pass
from agent_tools.chair_types import Action, Facts
from tests.test_chair_plan_kinds import _NOW as _KINDS_NOW
from tests.test_chair_plan_kinds import _RED_MAIN, _SCENARIOS, _facts, _stale_row

_SUBSETS = [frozenset(c) for n in range(len(chair_kinds.QUEUE_KINDS) + 1) for c in itertools.combinations(chair_kinds.QUEUE_KINDS, n)]


def _bag(actions: list[Action]) -> Counter[str]:
    """The actions as a multiset, without the lease epoch each planner stamps its own way."""
    return Counter(json.dumps({k: v for k, v in a.items() if k != "epoch"}, sort_keys=True, default=sorted) for a in actions)


_LEASE_NOW = "2026-11-20T12:00:00Z"


def _queued(facts: Facts, kinds: frozenset[str], *, by_lease: bool = False) -> Facts:
    """The facts with `kinds` listed in the profile, or (`by_lease`) held by a live worker lease only."""
    live = dict.fromkeys(kinds, "2026-11-20T12:01:00Z")
    if by_lease:
        return {**facts, "queue_leases": live, "queue_lease_now": _LEASE_NOW}  # type: ignore[return-value]
    return {**facts, "chair_queues": sorted(kinds), "queue_leases": live, "queue_lease_now": _LEASE_NOW}  # type: ignore[return-value]


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
@pytest.mark.parametrize("kinds", _SUBSETS, ids=lambda k: "+".join(sorted(k)) or "none")
def test_the_tick_and_the_acting_workers_together_plan_what_the_tick_alone_did(name: str, kinds: frozenset[str]):
    facts, now = _SCENARIOS[name]
    alone = plan_tick(facts, now)
    on = _queued(facts, kinds)
    workers = [a for kind in chair_kinds.QUEUE_KINDS for a in (planned_pass(kind, on, now, 1, dry_run=False) or [])]
    # The one thing left out: while a worker owns an initiative's triage, the tick holds its relaunch pair for a tick.
    assert _bag([*plan_tick(on, now), *workers]) == _bag(alone) - _bag(_held_for_triage(alone, kinds))


def _held_for_triage(alone: list[Action], kinds: frozenset[str]) -> list[Action]:
    """The relaunches and branch clears the tick alone planned for an initiative it also triaged or held, when a
    quarantine worker owns that triage."""
    if "quarantine" not in kinds:
        return []
    triaged = {a.get("initiative") for a in alone if a["kind"] in ("request_triage", "hold_task")}
    return [a for a in alone if a["kind"] in ("relaunch", "launch_epic", "clear_branches") and a.get("initiative") in triaged]


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
@pytest.mark.parametrize("kind", chair_kinds.QUEUE_KINDS)
def test_a_kind_is_planned_by_exactly_one_of_the_tick_and_its_worker(name: str, kind: str):
    facts, now = _SCENARIOS[name]
    mine = _bag(plan_kind(kind, facts, now))
    for kinds in _SUBSETS:
        on = _queued(facts, kinds)
        worker = planned_pass(kind, on, now, 1, dry_run=False)
        tick_left = _bag(plan_tick(on, now))
        assert (worker is not None) == (kind in kinds)
        if kind in kinds:
            assert _bag(worker or []) == mine and not (mine & tick_left), kinds
        else:
            assert mine <= tick_left, kinds


def test_a_live_worker_lease_takes_the_kind_off_the_tick_even_where_the_setting_is_off():
    facts = _facts(landed_main=[_RED_MAIN], stale_candidates=[_stale_row("a", "a-1")])
    held = _queued(facts, frozenset({"watch"}), by_lease=True)
    kinds = [a["kind"] for a in plan_tick(held, _KINDS_NOW)]
    assert "revert_land" in [a["kind"] for a in plan_tick(facts, _KINDS_NOW)]
    assert "revert_land" not in kinds and "stale_to_draft" in kinds


def test_with_both_kinds_queued_the_tick_plans_no_housekeeping_and_no_revert():
    facts = _facts(landed_main=[_RED_MAIN], stale_candidates=[_stale_row("a", "a-1")])
    on = _queued(facts, frozenset(chair_kinds.QUEUE_KINDS))
    assert {"revert_land", "stale_to_draft", "housekeeping"} <= {a["kind"] for a in plan_tick(facts, _KINDS_NOW)}
    assert {"revert_land", "stale_to_draft", "housekeeping"}.isdisjoint(a["kind"] for a in plan_tick(on, _KINDS_NOW))
    assert [a["kind"] for a in plan_kind("watch", on, _KINDS_NOW)] == ["revert_land"]
    assert [a["kind"] for a in plan_kind("housekeeping", on, _KINDS_NOW)] == ["stale_to_draft", "housekeeping"]


def test_a_worker_whose_kind_is_not_queued_plans_nothing_live_but_its_dry_run_still_plans():
    facts = _facts(landed_main=[_RED_MAIN])
    assert planned_pass("watch", facts, _KINDS_NOW, 3, dry_run=False) is None
    assert [(a["kind"], a["epoch"]) for a in planned_pass("watch", facts, _KINDS_NOW, -1, dry_run=True) or []] == [("revert_land", -1)]


def test_a_worker_stamps_each_action_with_its_own_lease_epoch():
    on = _queued(_facts(landed_main=[_RED_MAIN]), frozenset({"watch"}))
    assert [a["epoch"] for a in planned_pass("watch", on, _KINDS_NOW, 12, dry_run=False) or []] == [12]


def test_a_kind_with_no_worker_plans_nothing():
    facts, now = _SCENARIOS["launch fills free lanes"]
    assert plan_kind("launch", facts, now) == []


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
def test_queuing_never_changes_the_alarms_the_tick_raises(name: str):
    """The stall alarm reads every kind's slots, a worker's too, so moving housekeeping never raises a second idle line."""
    facts, now = _SCENARIOS[name]
    alarms = {"stall_alarm", "stall_clear"}
    off = [a for a in plan_tick(facts, now) if a["kind"] in alarms]
    on = [a for a in plan_tick(_queued(facts, frozenset(chair_kinds.QUEUE_KINDS)), now) if a["kind"] in alarms]
    assert _bag(on) == _bag(off)


def test_a_lease_taken_after_the_gather_takes_the_kind_off_the_tick_when_it_plans_again_before_perform():
    from agent_tools.chair_run import queue_facts
    from agent_tools.chair_types import QueueLeases

    facts = _facts(landed_main=[_RED_MAIN], stale_candidates=[_stale_row("a", "a-1")])
    gathered = queue_facts(facts, QueueLeases({}, _LEASE_NOW, False, 0.0))
    assert "revert_land" in [a["kind"] for a in plan_tick(gathered, _KINDS_NOW)]
    fresh = queue_facts(gathered, QueueLeases({"watch": "2026-11-20T12:01:00Z"}, _LEASE_NOW, True, 0.0))
    assert chair_kinds.tick_skips(fresh) - chair_kinds.tick_skips(gathered) == {"watch"}
    assert "revert_land" not in [a["kind"] for a in plan_tick(fresh, _KINDS_NOW)]


def test_a_listed_kind_with_no_worker_puts_one_needs_chair_on_the_tick():
    facts = {**_facts(), "chair_queues": ["watch"], "queue_leases": {}, "queue_lease_now": _LEASE_NOW}
    causes = [a.get("cause") for a in plan_tick(facts, _KINDS_NOW) if a["kind"] == "needs_chair"]  # type: ignore[arg-type]
    assert causes.count("queue_unowned watch") == 1


# C4: quarantine and decompose.

_TRIAGE = "triage, a hold and a retry beside a decompose"


def test_with_quarantine_and_decompose_queued_the_tick_makes_no_triage_and_launches_no_decompose():
    facts, now = _SCENARIOS[_TRIAGE]
    on = _queued(facts, frozenset({"quarantine", "decompose"}))
    alone = [a["kind"] for a in plan_tick(facts, now)]
    left = [a["kind"] for a in plan_tick(on, now)]
    assert {"request_triage", "hold_task", "launch_decompose", "retry"} <= set(alone)
    assert {"request_triage", "hold_task", "launch_decompose"}.isdisjoint(left)
    assert "retry" in left  # a retry starts a run: it stays with launch until C5
    assert [a["kind"] for a in plan_kind("quarantine", on, now)] == ["request_triage", "hold_task"]
    assert [a["kind"] for a in plan_kind("decompose", on, now)] == ["launch_decompose"]


def _strip(actions: list[Action]) -> list[Action]:
    return [{k: v for k, v in a.items() if k != "queue_kind"} for a in actions]  # type: ignore[misc]


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
@pytest.mark.parametrize("kinds", _SUBSETS, ids=lambda k: "+".join(sorted(k)) or "none")
def test_each_action_the_tick_keeps_is_tagged_with_the_kind_whose_worker_would_plan_it(name: str, kinds: frozenset[str]):
    """The tag comes from the slot that planned the action: per kind, the tick's tagged actions are exactly what that
    kind's worker would plan, and none names a kind the tick skipped."""
    facts, now = _SCENARIOS[name]
    on = _queued(facts, kinds)
    planned = plan_tick(on, now)
    tagged = tag_queue_kinds(planned, on, now)
    assert _strip(tagged) == planned
    for kind in chair_kinds.QUEUE_KINDS:
        mine = [a for a in tagged if a.get("queue_kind") == kind]
        assert _bag(_strip(mine)) == (Counter() if kind in kinds else _bag(plan_kind(kind, on, now))), kind


def test_a_plan_that_does_not_line_up_is_refused_rather_than_tagged_by_its_text():
    facts, now = _SCENARIOS[_TRIAGE]
    planned = plan_tick(facts, now)
    triage = next(a for a in planned if a["kind"] == "request_triage")
    unknown = {"kind": "needs_chair", "initiative": "zz", "cause": "c", "epoch": triage.get("epoch")}
    with pytest.raises(PlanMismatch):
        tag_queue_kinds([unknown, triage], facts, now)  # type: ignore[list-item]


# The tick's own order for a first failed check (`test_chair_quarantine_triage`): triage, then the relaunch pair, so
# the relaunched run picks the triaged task up.
_I_READY = {"id": "i", "started": True, "ready_tasks": [{"id": "t2", "needs": []}], "landed": set()}
_T1 = {"task_id": "t1", "initiative": "i", "cause": "code", "harness_failures": 0, "run": "i-4", "failures": 1}


def _for_i(actions: list[Action]) -> list[str]:
    return [str(a["kind"]) for a in actions if a.get("initiative") == "i"]


def test_with_quarantine_queued_the_relaunch_still_comes_after_the_triage_one_tick_later():
    before = _facts(initiatives=[_I_READY], run_exited={"i": True}, quarantines=[_T1])
    alone = _for_i(plan_tick(before, _KINDS_NOW))
    assert alone == ["request_triage", "clear_branches", "relaunch"]
    # Tick 1 and the worker, from the same store: the worker triages; the tick holds the relaunch pair back.
    on = _queued(before, frozenset({"quarantine"}))
    tick_1, worker_1 = plan_tick(on, _KINDS_NOW), planned_pass("quarantine", on, _KINDS_NOW, 1, dry_run=False) or []
    assert _for_i(tick_1) == [] and _for_i(worker_1) == ["request_triage"]
    # The worker's write: the task is back to ready, flagged, and its triage key handled. The next tick relaunches.
    key = next(a["key"] for a in worker_1 if a["kind"] == "request_triage")
    after = _queued(
        _facts(
            initiatives=[{**_I_READY, "ready_tasks": [*_I_READY["ready_tasks"], {"id": "t1", "needs": []}]}],
            run_exited={"i": True}, handled_keys=[key],
        ),
        frozenset({"quarantine"}),
    )
    tick_2, worker_2 = plan_tick(after, _KINDS_NOW), planned_pass("quarantine", after, _KINDS_NOW, 1, dry_run=False) or []
    assert _for_i(worker_2) == []
    # Performed in the order the processes run (tick 1, the worker, tick 2), the events are the tick alone's order.
    assert [*_for_i(tick_1), *_for_i(worker_1), *_for_i(tick_2)] == alone


def test_a_triage_the_tick_still_makes_itself_holds_nothing_back():
    facts = _facts(initiatives=[_I_READY], run_exited={"i": True}, quarantines=[_T1])
    for kinds in _SUBSETS:
        if "quarantine" not in kinds:
            assert _for_i(plan_tick(_queued(facts, kinds), _KINDS_NOW)) == ["request_triage", "clear_branches", "relaunch"], kinds


def test_a_quarantine_reaching_the_chair_is_the_triages_but_the_same_text_from_elsewhere_is_the_launchs():
    reach = {"kind": "needs_chair", "initiative": "q", "task_id": "q-t", "cause": "scope"}
    assert launch_slot_kind(reach, [reach]) == "quarantine"  # type: ignore[arg-type]
    assert launch_slot_kind(reach, []) == "launch"  # type: ignore[arg-type]
    assert launch_slot_kind({"kind": "launch_decompose", "intake_ids": ["n1"]}, []) == "decompose"  # type: ignore[arg-type]
    for kind in ("request_triage", "hold_task", "announce_hold"):
        assert launch_slot_kind({"kind": kind, "task_id": "t", "initiative": "i"}, []) == "quarantine"  # type: ignore[arg-type]
    for kind in ("relaunch", "retry", "rescue", "rebuild_quarantined", "launch_epic", "pull", "rebase_phase"):
        assert launch_slot_kind({"kind": kind, "initiative": "i"}, []) == "launch"  # type: ignore[arg-type]
