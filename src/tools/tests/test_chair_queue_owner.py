"""C3: at any moment exactly one of the tick and a kind's queue worker plans that kind, and the two together plan what
the tick alone did.

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
from agent_tools.chair_plan import plan_kind, plan_tick
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
    assert _bag([*plan_tick(on, now), *workers]) == _bag(alone)


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

    facts = _facts(landed_main=[_RED_MAIN], stale_candidates=[_stale_row("a", "a-1")])
    gathered = queue_facts(facts, ({}, _LEASE_NOW))
    assert "revert_land" in [a["kind"] for a in plan_tick(gathered, _KINDS_NOW)]
    fresh = queue_facts(gathered, ({"watch": "2026-11-20T12:01:00Z"}, _LEASE_NOW))
    assert chair_kinds.tick_skips(fresh) - chair_kinds.tick_skips(gathered) == {"watch"}
    assert "revert_land" not in [a["kind"] for a in plan_tick(fresh, _KINDS_NOW)]


def test_a_listed_kind_with_no_worker_puts_one_needs_chair_on_the_tick():
    facts = {**_facts(), "chair_queues": ["watch"], "queue_leases": {}, "queue_lease_now": _LEASE_NOW}
    causes = [a.get("cause") for a in plan_tick(facts, _KINDS_NOW) if a["kind"] == "needs_chair"]  # type: ignore[arg-type]
    assert causes.count("queue_unowned watch") == 1
