import json
from datetime import UTC, datetime, timedelta

import pytest

from agent_tools.chair_exec import Deps, perform
from agent_tools.chair_plan import plan_tick
from agent_tools.chair_plan_fill import plan_fill, schedule_for
from agent_tools.chair_schedule import SCHEDULE_FILE, schedule_feed
from agent_tools.chair_types import Facts

NOW = datetime(2026, 11, 9, 8, 0, tzinfo=UTC)
SHARED = "src/shared.py"


def _phase(surfaces: list[str]) -> dict:
    return {"id": "p1", "status": "ready", "parents": [], "size": "S", "mix": "plain", "surfaces": surfaces}


def _slot(name: str) -> dict:
    return {"name": name, "lanes": [f"{name}-1"], "local": False, "repos": []}


# a and b hold SHARED strictly, so b waits for a; c is independent. Two hosts, one lane each.
INPUTS = {
    "initiatives": [
        {"id": "a", "priority": 3, "repo": "r", "phases": [_phase([SHARED])]},
        {"id": "b", "priority": 2, "repo": "r", "phases": [_phase([SHARED])]},
        {"id": "c", "priority": 1, "repo": "r", "phases": [_phase(["src/other.py"])]},
    ],
    "records": [],
    "hosts": [_slot("host-a"), _slot("host-b")],
    "mode": "building",
    "load": {"host-a": 0, "host-b": 0},
    "target": None,
}


def _init(id: str, surfaces: list[str]) -> dict:
    return {
        "id": id,
        "started": False,
        "ready_tasks": [{"id": "t1", "needs": []}],
        "landed": set(),
        "repo": "r",
        "ready_surfaces": surfaces,
    }


def _facts(**over) -> Facts:
    base = {
        "lease": {"holder": "h", "host": "x", "epoch": 1, "mine": True, "released": False, "stale": False},
        "limits": {
            "hard_stop": False,
            "weekly_fraction": 0.0,
            "hard_stop_fraction": 1.0,
            "launch_cap": 4,
            "go_degraded": False,
        },
        "dispatch": {
            "max_in_flight": 0,
            "live_runs": 0,
            "hosts": [
                {"name": "host-a", "capacity": 1, "live_runs": 0},
                {"name": "host-b", "capacity": 1, "live_runs": 0},
            ],
        },
        "approved": [],
        "initiatives": [_init("a", [SHARED]), _init("b", [SHARED]), _init("c", ["src/other.py"])],
        "quarantines": [],
        "intake": [],
        "work_store_ready": True,
        "sources_configured": True,
        "remote_unfetched": {},
        "run_exited": {},
        "schema_deaths": {},
        "lost_runs": {},
        "last_housekeeping_at": None,
        "housekeeping_hours": 24.0,
        "stale_days": 7,
        "stale_candidates": [],
        "stall_candidates": [],
        "home": {},
        "schedule_inputs": INPUTS,
    }
    return {**base, **over}


def _launches(actions: list[dict]) -> list[tuple[str, str | None]]:
    return [(a["initiative"], a.get("host")) for a in actions if a["kind"] == "launch_epic"]


def _scheduled(inputs: dict = INPUTS) -> list[tuple[str, str | None]]:
    return [(p.key[0], p.host) for p in schedule_for(inputs, NOW).launches]


def test_launches_equal_the_schedules_this_tick_list_in_order_and_host():
    scheduled = _scheduled()
    assert scheduled == [("a", "host-a"), ("c", "host-b")]
    assert _launches(plan_tick(_facts(), NOW)) == scheduled


def test_a_withheld_launch_is_dropped_and_no_unscheduled_initiative_replaces_it():
    actions = plan_tick(_facts(schema_deaths={"a": "schema version death"}), NOW)
    assert _launches(actions) == [("c", "host-b")]


def test_a_scheduled_launch_without_a_free_host_lane_is_dropped():
    busy = {"name": "host-b", "capacity": 1, "live_runs": 1}
    facts = _facts(
        dispatch={
            "max_in_flight": 0,
            "live_runs": 1,
            "hosts": [{"name": "host-a", "capacity": 1, "live_runs": 0}, busy],
        }
    )
    assert _launches(plan_tick(facts, NOW)) == [("a", "host-a")]


def _updates(actions: list[dict]) -> list[dict]:
    return [a for a in actions if a["kind"] == "schedule_updated"]


# Three minutes and a bit later: the clock has moved, the plan has not.
LATER = NOW + timedelta(minutes=3, seconds=17, microseconds=400_000)
MISSES = {
    **INPUTS,
    "target": "2026-11-09T08:30:00+00:00",
}  # the default estimates finish well after it, so advice appears


@pytest.mark.parametrize("inputs", [INPUTS, MISSES], ids=["no-target", "missed-target-with-advice"])
def test_an_unchanged_schedule_appends_no_second_schedule_updated_action_as_the_clock_moves(inputs):
    first = plan_tick(_facts(schedule_inputs=inputs), NOW)
    assert len(_updates(first)) == 1
    previous = schedule_feed(schedule_for(inputs, NOW), NOW)
    assert (
        previous["schedule"]["lanes"][0]["next"][0]["start"]
        != schedule_feed(schedule_for(inputs, LATER), LATER)["schedule"]["lanes"][0]["next"][0]["start"]
    ), "the absolute stamps do move with the clock"
    again = plan_tick(_facts(schedule_inputs=inputs, previous_schedule_feed=previous), LATER)
    assert _updates(again) == []


def test_a_missed_target_advice_is_in_the_feed_being_compared():
    assert schedule_feed(schedule_for(MISSES, NOW), NOW)["schedule"]["advice"][0]["kind"] == "miss"


def test_a_changed_schedule_appends_one_schedule_updated_action():
    stale_feed = schedule_feed(schedule_for({**INPUTS, "initiatives": INPUTS["initiatives"][2:]}, NOW), NOW)
    actions = plan_tick(_facts(previous_schedule_feed=stale_feed), NOW)
    assert len(_updates(actions)) == 1


def test_a_changed_schedule_still_appends_when_the_clock_has_also_moved():
    stale_feed = schedule_feed(schedule_for({**INPUTS, "initiatives": INPUTS["initiatives"][2:]}, NOW), NOW)
    assert len(_updates(plan_tick(_facts(previous_schedule_feed=stale_feed), LATER))) == 1


def test_a_missing_previous_feed_appends_the_action_and_perform_writes_the_document(tmp_path):
    update = _updates(plan_tick(_facts(), NOW))
    assert len(update) == 1
    deps = Deps(
        run=lambda argv: (0, ""),
        delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "",
        record=lambda action: None,
        run_id=lambda a: "run-1",
        repo_for=lambda a: "r",
        runs_dir=tmp_path,
    )
    assert perform(update, deps, lambda: 1, False)[0]["status"] == "done"
    assert json.loads((tmp_path / SCHEDULE_FILE).read_text()) == update[0]["schedule"]


def _local_facts(**over) -> Facts:
    local = {"name": "chair", "lanes": ["chair-1"], "local": True, "repos": []}
    inputs = {**INPUTS, "hosts": [local, _slot("host-a")], "load": {"chair": 0, "host-a": 0}}
    dispatch = {"max_in_flight": 2, "live_runs": 0, "hosts": [{"name": "host-a", "capacity": 1, "live_runs": 0}]}
    return _facts(schedule_inputs=inputs, dispatch=dispatch, intake=["i1"], **over)


def test_a_scheduled_local_launch_takes_a_local_lane_before_a_decompose_does():
    facts = _local_facts()
    schedule = schedule_for(facts["schedule_inputs"], NOW)
    assert [(p.key[0], p.host) for p in schedule.launches] == [("a", "chair"), ("c", "host-a")]
    assert plan_fill(facts, 1, now=NOW, schedule=schedule) == [
        {"kind": "launch_epic", "initiative": "a"},
        {"kind": "launch_epic", "initiative": "c", "host": "host-a"},
    ]
    both = plan_fill(facts, 2, now=NOW, schedule=schedule)
    assert [(a["kind"], a.get("initiative"), a.get("host")) for a in both] == [
        ("launch_epic", "a", None),
        ("launch_epic", "c", "host-a"),
        ("launch_decompose", None, None),
    ]


def test_absent_inputs_reproduce_the_old_ranking_and_append_no_schedule_action():
    facts = _facts()
    del facts["schedule_inputs"]
    facts["dispatch"] = {"max_in_flight": 4, "live_runs": 0, "hosts": []}
    facts["initiatives"] = [_init("a", []), _init("b", [])]
    assert plan_fill(facts, 3) == [
        {"kind": "launch_epic", "initiative": "a"},
        {"kind": "launch_epic", "initiative": "b"},
    ]
    assert "schedule_updated" not in [a["kind"] for a in plan_tick(facts, NOW)]
