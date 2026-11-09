from datetime import UTC, datetime

from test_chair_plan_fill_steer import _facts as _fill_facts
from test_chair_plan_fill_steer import _init, _launch
from test_chair_plan_launch_budget import _facts as _base_facts

from agent_tools.chair_plan import plan_tick
from agent_tools.chair_plan_fill import plan_fill
from agent_tools.chair_schedule import schedule_feed
from agent_tools.chair_types import Facts
from agent_tools.route_schedule import plan_from_inputs

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
HOSTS = [
    {"name": "chair", "lanes": ["l1"], "local": True, "repos": []},
    {"name": "host-a", "lanes": ["l1"], "local": False, "repos": []},
    {"name": "host-b", "lanes": ["l1"], "local": False, "repos": []},
]
# a and b hold src/shared.py between them; c is disjoint.
SURFACES = {"a": ["src/shared.py"], "b": ["src/shared.py"], "c": ["src/other.py"]}


def _initiative(id: str) -> dict:
    task = {"id": f"{id}-t", "needs": []}
    return {"id": id, "repo": "r", "started": False, "ready_tasks": [task], "landed": set(), "ready_surfaces": SURFACES[id]}


def _phase(id: str) -> dict:
    return {"id": "p1", "status": "ready", "parents": [], "size": "S", "mix": "plain", "surfaces": SURFACES[id]}


def _inputs() -> dict:
    return {
        "initiatives": [{"id": i, "priority": n, "repo": "r", "phases": [_phase(i)]} for n, i in enumerate("abc")],
        "records": [],
        "hosts": HOSTS,
        "mode": "orchestrating",
        "load": {},
        "target": None,
    }


def _facts(**over) -> Facts:
    dispatch = {"max_in_flight": 3, "live_runs": 0, "hosts": [{"name": "host-a", "live_runs": 0, "capacity": 1}, {"name": "host-b", "live_runs": 0, "capacity": 1}]}
    return _base_facts(
        initiatives=[_initiative(i) for i in "abc"],
        dispatch=dispatch,
        steer_policy={"limit": 1, "serial": []},
        schedule_inputs=_inputs(),
        **over,
    )


def _epics(actions: list[dict]) -> list[tuple[str, str | None]]:
    return [(a["initiative"], a.get("host")) for a in actions if a["kind"] == "launch_epic"]


def _scheduled(facts: Facts) -> list[tuple[str, str]]:
    return [(p.key[0], p.host) for p in plan_from_inputs(facts["schedule_inputs"], NOW)[0].launches]


def test_the_chairs_launches_are_the_schedules_this_tick_list_in_order_and_host():
    facts = _facts()
    expected = _scheduled(facts)
    ids = [i for i, _ in expected]
    assert len(ids) == 2 and "c" in ids and len({"a", "b"} & set(ids)) == 1  # one holder of the shared file launches
    assert len({h for _, h in expected}) == 2
    assert _epics(plan_tick(facts, NOW)) == expected


def test_a_withheld_launch_is_dropped_and_nothing_takes_its_place():
    facts = _facts()
    schedule = plan_from_inputs(facts["schedule_inputs"], NOW)[0]
    first, second = _scheduled(facts)
    filled = plan_fill(facts, 3, frozenset({first[0]}), now=NOW, schedule=schedule)
    assert _epics(filled) == [second]


def test_an_unchanged_schedule_appends_no_second_schedule_updated_action():
    facts = _facts()
    first = [a for a in plan_tick(facts, NOW) if a["kind"] == "schedule_updated"]
    assert len(first) == 1
    feed = schedule_feed(plan_from_inputs(facts["schedule_inputs"], NOW)[0], NOW)
    again = {**facts, "previous_schedule_feed": feed}
    assert [a for a in plan_tick(again, NOW) if a["kind"] == "schedule_updated"] == []


def test_absent_inputs_reproduce_the_old_plan():
    facts = _fill_facts(initiatives=[_init("x", ["src/a.rs"]), _init("z", ["src/b.rs"])])
    assert plan_fill(facts, 2) == [_launch("x"), _launch("z")]
    assert [a for a in plan_tick(_facts_without_inputs(), NOW) if a["kind"] == "schedule_updated"] == []


def _facts_without_inputs() -> Facts:
    return {k: v for k, v in _facts().items() if k != "schedule_inputs"}  # type: ignore[return-value]
