"""`policy.dispatch.lane_model`: the setting, its stamp on the initiative row at decompose, and the chair's read of it.

Every test here is pure: plain rows and facts in, plain rows and actions out, no store and no patching."""

from datetime import UTC, datetime

from agent_tools import queue_rows, route, route_status, store_fill
from agent_tools.chair_idle_ready import plan_idle_ready
from agent_tools.chair_plan import plan_tick
from agent_tools.chair_plan_fill import less_phase_ready, plan_fill
from agent_tools.chair_read_docket import docket_from_rows
from agent_tools.chair_schedule import Placement, Schedule
from agent_tools.chair_stall_alarm import plan_stall_alarm, resolve_settings
from agent_tools.settings_model import setting_for
from agent_tools.settings_plan import Plan, PlanError, plan_set
from agent_tools.settings_sections.defaults import default_rows
from tests.test_chair_plan import _facts as _tick_facts
from tests.test_chair_plan import _unstarted

PLAN = {
    "id": "init-a", "title": "A", "body": "about A",
    "phases": [{"id": "p1", "goal": "g"}],
    "tasks": [{"id": "t1", "phase": "p1", "title": "T1", "body": "x", "needs": [], "surfaces": []}],
}
INTAKE = {
    "kind": "intake", "initiative": "", "task_id": "I1", "phase": "", "state": "queued",
    "needs": [], "title": "Idea", "surfaces": [], "body": "the idea", "extra": {"id": "I1"},
}
INITIATIVE = {
    "kind": "initiative", "initiative": "init-a", "task_id": "init-a", "phase": "", "state": "todo", "needs": [],
    "title": "A", "surfaces": [], "body": "why", "extra": {"id": "init-a"},
}
TASK = {
    "kind": "task", "initiative": "init-a", "task_id": "t1", "phase": "p1", "state": "ready", "needs": [],
    "title": "T1", "surfaces": [], "body": "", "extra": {"id": "t1"}, "holder": None, "epoch": 0, "expires_at": "",
}
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def _stamped(row: dict) -> dict:
    return {**row, "extra": {**row["extra"], "lane_model": "phase"}}


# The setting.


def test_the_setting_is_registered_with_default_task():
    setting = setting_for("cartridge", "policy.dispatch.lane_model")
    assert (setting.section, setting.source_file) == ("lanes and machines", "cartridge.yaml")
    by_key = {r["key"]: r for r in default_rows([])}
    assert by_key["policy.dispatch.lane_model"]["value"] == "task"


def test_task_and_phase_set_and_anything_else_is_refused():
    for value in ("task", "phase"):
        plan = plan_set("cartridge", "policy.dispatch.lane_model", value, "name: team\n")
        assert isinstance(plan, Plan)
        assert f"lane_model: {value}" in plan.new_text
    refused = plan_set("cartridge", "policy.dispatch.lane_model", "worker", "name: team\n")
    assert refused == PlanError("policy.dispatch.lane_model must be one of task, phase, got 'worker'")


def test_the_cartridge_value_reads_phase_only_when_it_says_phase():
    assert queue_rows.cartridge_lane_model({"policy": {"dispatch": {"lane_model": "phase"}}}) == "phase"
    assert queue_rows.cartridge_lane_model({"policy": {"dispatch": {"lane_model": "task"}}}) == "task"
    assert queue_rows.cartridge_lane_model({"policy": {"dispatch": {"lane_model": "worker"}}}) == "task"
    assert queue_rows.cartridge_lane_model({"policy": {"dispatch": "phase"}}) == "task"
    assert queue_rows.cartridge_lane_model({}) == "task"


# The row codec.


def test_lane_model_of_row_reads_absent_and_unknown_as_task():
    assert queue_rows.lane_model_of_row(_stamped(INITIATIVE)) == "phase"
    assert queue_rows.lane_model_of_row(INITIATIVE) == "task"
    assert queue_rows.lane_model_of_row({**INITIATIVE, "extra": {"lane_model": "worker"}}) == "task"
    assert queue_rows.lane_model_of_row({**INITIATIVE, "extra": None}) == "task"
    assert queue_rows.lane_model_of_row(_stamped(TASK)) == "task"  # only an initiative row carries the model


def test_plan_to_rows_stamps_phase_on_the_initiative_row_alone():
    head, task = queue_rows.plan_to_rows(PLAN, "I1", "phase")
    assert head["extra"]["lane_model"] == "phase"
    assert "lane_model" not in task["extra"]
    assert queue_rows.lane_model_of_row(head) == "phase"


def test_plan_to_rows_with_task_writes_no_key_and_matches_the_default():
    rows = queue_rows.plan_to_rows(PLAN, "I1", "task")
    assert "lane_model" not in rows[0]["extra"]
    assert rows == queue_rows.plan_to_rows(PLAN, "I1")


def test_a_stamp_is_never_changed_and_restamping_is_a_no_op():
    once = queue_rows.stamp_lane_model(INITIATIVE, "phase")
    assert queue_rows.stamp_lane_model(once, "phase") == once
    kept = {**INITIATIVE, "extra": {"id": "init-a", "lane_model": "task"}}
    assert queue_rows.stamp_lane_model(kept, "phase") == kept
    assert queue_rows.stamp_lane_model(TASK, "phase") == TASK


def test_the_stamp_round_trips_through_the_file_codec():
    (head, *_) = queue_rows.plan_to_rows(PLAN, "I1", "phase")
    parsed = queue_rows.parse_item("initiative", ("init-a", "initiative.md"), queue_rows.render_item(head))
    assert queue_rows.lane_model_of_row(parsed) == "phase"


# The decompose row writers.


def test_plan_writes_stamps_a_new_initiative_and_repeats_the_same_rows():
    rows, failure = route.plan_writes(PLAN, INTAKE, [], "phase")
    assert failure is None
    assert queue_rows.lane_model_of_row(rows[0]) == "phase"
    assert route.plan_writes(PLAN, INTAKE, [], "phase") == (rows, None)


def test_plan_writes_keeps_the_model_a_stored_initiative_was_decomposed_under():
    rows, _ = route.plan_writes(PLAN, INTAKE, [INITIATIVE], "phase")
    assert "lane_model" not in rows[0]["extra"]
    rows, _ = route.plan_writes(PLAN, INTAKE, [_stamped(INITIATIVE)], "task")
    assert queue_rows.lane_model_of_row(rows[0]) == "phase"


def test_store_fill_stamps_an_inserted_initiative_only():
    plan = store_fill.plan_fill([INITIATIVE, TASK], [], "phase")
    head, task = plan.writes
    assert queue_rows.lane_model_of_row(head) == "phase"
    assert "lane_model" not in task["extra"]
    assert store_fill.plan_fill([INITIATIVE, TASK], [], "task").writes == (INITIATIVE, TASK)


def test_store_fill_never_restamps_a_stored_initiative_and_a_repeat_fill_writes_nothing():
    assert store_fill.plan_fill([INITIATIVE], [INITIATIVE], "phase").writes == ()
    stamped = store_fill.plan_fill([INITIATIVE], [], "phase").writes
    assert store_fill.plan_fill([INITIATIVE], list(stamped), "phase").writes == ()


def test_store_fill_keeps_the_model_the_file_names():
    named = {**INITIATIVE, "extra": {"id": "init-a", "lane_model": "task"}}
    (head,) = store_fill.plan_fill([named], [], "phase").writes
    assert queue_rows.lane_model_of_row(head) == "task"


# The chair.


def test_the_docket_carries_phase_only_for_a_stamped_initiative():
    (entry,) = docket_from_rows([_stamped(INITIATIVE), TASK], "2026-11-20T00:00:00Z")
    assert entry["lane_model"] == "phase"
    (entry,) = docket_from_rows([INITIATIVE, TASK], "2026-11-20T00:00:00Z")
    assert "lane_model" not in entry


def test_launchable_by_tick_refuses_phase_alone():
    assert queue_rows.launchable_by_tick(_unstarted("a"))
    assert queue_rows.launchable_by_tick({**_unstarted("a"), "lane_model": "task"})
    assert not queue_rows.launchable_by_tick({**_unstarted("a"), "lane_model": "phase"})


def _fill_facts(*initiatives: dict) -> dict:
    return _tick_facts(initiatives=list(initiatives), dispatch={"max_in_flight": 4, "live_runs": 0, "hosts": []})


def test_fill_launches_a_task_initiative_and_never_a_phase_one():
    facts = _fill_facts({**_unstarted("a"), "lane_model": "phase"}, _unstarted("b"))
    assert plan_fill(facts, 3) == [{"kind": "launch_epic", "initiative": "b"}]


def test_the_scheduled_path_drops_a_phase_initiative_too():
    facts = {
        **_fill_facts({**_unstarted("a"), "lane_model": "phase"}, _unstarted("b")),
        "schedule_inputs": {"hosts": [{"name": "host-a", "local": False}]},
        "dispatch": {"max_in_flight": 4, "live_runs": 0, "hosts": [{"name": "host-a", "live_runs": 0, "capacity": 4}]},
    }
    placed = tuple(Placement((i, "p1"), "host-a", "l1", NOW, NOW, False) for i in ("a", "b"))
    schedule = Schedule(placed, (), (), None, None, None, (), (), ())
    assert plan_fill(facts, 0, schedule=schedule) == [{"kind": "launch_epic", "initiative": "b", "host": "host-a"}]


def test_the_tick_names_a_phase_initiative_as_waiting_for_a_phase_worker():
    actions = plan_tick(_tick_facts(initiatives=[{**_unstarted("a"), "lane_model": "phase"}, _unstarted("b")]), NOW)
    assert [a["initiative"] for a in actions if a["kind"] == "launch_epic"] == ["b"]
    assert {a["initiative"]: a["reason"] for a in actions if a["kind"] == "unlaunched"} == {"a": "waits for a phase worker"}


# The launch stub: the chair decides the model once, when it writes `initiative.md`.


def test_the_stub_carries_phase_and_nothing_under_task():
    stamped = route.initiative_text("init-a", "A", "app", "intake/I1.md", "about A", "phase")
    assert route.parse_frontmatter(stamped)[0]["lane_model"] == "phase"
    plain = route.initiative_text("init-a", "A", "app", "intake/I1.md", "about A", "task")
    assert "lane_model" not in route.parse_frontmatter(plain)[0]
    assert plain == route.initiative_text("init-a", "A", "app", "intake/I1.md", "about A")


def test_a_launch_reads_the_stub_before_the_cartridge():
    assert queue_rows.launch_lane_model(None, "phase") == "phase"
    assert queue_rows.launch_lane_model(None, "task") == "task"
    assert queue_rows.launch_lane_model({"lane_model": "phase"}, "task") == "phase"
    assert queue_rows.launch_lane_model({"id": "init-a"}, "phase") == "task"  # a stub with no key was written under task


def test_a_stub_the_export_fills_with_no_model_then_fetched_and_planned_still_reads_phase():
    stub = route.initiative_text("init-a", "A", "app", "intake/I1.md", "about A", "phase")
    files = [(("work", "init-a", "initiative.md"), stub)]
    parsed, _ = store_fill.parse_workspace(files)
    (row,) = store_fill.plan_fill(parsed, []).writes  # the export's fill passes no model
    assert queue_rows.lane_model_of_row(row) == "phase"
    fetched, _ = store_fill.parse_workspace([*files, (("work", "init-a", "p1", "t1.md"), "---\nid: t1\n---\nx\n")])
    assert [w["task_id"] for w in store_fill.plan_fill(fetched, [row], "task").writes] == ["t1"]
    planned, _ = route.plan_writes(PLAN, INTAKE, [row], "task")
    assert queue_rows.lane_model_of_row(planned[0]) == "phase"


# Status and alarms.


def test_status_names_each_phase_initiative_and_nothing_when_none():
    queue = [_stamped(INITIATIVE), {**_stamped(INITIATIVE), "task_id": "gone", "state": "dropped"}, TASK]
    assert route.phase_lane_initiatives(queue) == ["init-a"]
    assert route.phase_lane_initiatives([INITIATIVE, TASK]) == []
    text = route.render_status([], phase_lane=["init-a"])
    assert text.splitlines() == [route_status.PHASE_LANE_HEADING, "  init-a"]
    assert route.render_status([]) == ""


def test_the_idle_ready_alarm_leaves_out_a_phase_initiative():
    idle = {"last_progress_at": "2026-11-20T10:00:00+00:00", "open_signature": None}
    dispatch = {"max_in_flight": 2, "live_runs": 0, "hosts": []}
    phase = {"id": "a", "lane_model": "phase", "ready_tasks": [{"id": "t1"}]}
    facts = {"dispatch": dispatch, "idle_stall": idle, "initiatives": [phase]}
    assert plan_idle_ready(facts, [], NOW) == []
    (action,) = plan_idle_ready({**facts, "initiatives": [phase, {"id": "b", "ready_tasks": [{"id": "t2"}]}]}, [], NOW)
    assert action["signature"] == "idle_ready:b"


def test_the_idle_ready_count_drops_a_phase_initiative_s_tasks():
    inputs = {"free_lanes": 2, "ready": 3, "queued": 0}
    phase = {"id": "a", "lane_model": "phase", "ready_tasks": [{"id": "t1"}, {"id": "t2"}]}
    assert less_phase_ready(inputs, [phase, _unstarted("b")])["ready"] == 1
    assert less_phase_ready({**inputs, "ready": 1}, [phase])["ready"] == 0
    assert less_phase_ready(inputs, [_unstarted("b")]) is inputs


_ALARM = {
    "settings": resolve_settings({}),
    "last_land_at": None, "open_initiatives": {}, "last_launch_at": "2026-11-20T09:00:00Z",
    "watch_since": "2026-11-18T12:00:00Z", "heads": {}, "streaks": [], "open": {}, "told": {}, "stranded": {},
    "holds": [], "local_host": "chair",
}


def _no_launch(initiatives: list[dict], ready: int) -> list[tuple[str, str]]:
    idle = less_phase_ready({"free_lanes": 2, "ready": ready, "queued": 0, "open_signature": None}, initiatives)
    actions = plan_stall_alarm({"stall_alarm": _ALARM, "idle_stall": idle, "initiatives": initiatives}, [], NOW)
    return [(a["stall"], a["subject"]) for a in actions if a["kind"] == "stall_alarm"]


def test_the_no_launch_alarm_neither_fires_for_nor_names_a_phase_initiative():
    phase = {"id": "a", "lane_model": "phase", "ready_tasks": [{"id": "t1"}]}
    assert _no_launch([phase], 1) == []
    assert _no_launch([phase, {"id": "b", "ready_tasks": [{"id": "t2"}]}], 2) == [("no_launch", "b")]
