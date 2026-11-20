"""The chair's side of the phase queue, pure: which rows it wants, what it writes, and the tick action that carries it."""

from datetime import UTC, datetime

from agent_tools import chair_phase_queue
from agent_tools.chair_plan import plan_tick
from agent_tools.chair_plan_fill import less_phase_ready
from tests.test_chair_plan import _facts as _tick_facts
from tests.test_chair_plan import _unstarted

NOW = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)


def _task(id: str, phase: str, requires=()) -> dict:
    return {"id": id, "phase": phase, "needs": [], "requires": list(requires)}


def _phase_initiative(id: str = "ship", *phases: tuple[str, list[dict]]) -> dict:
    ready = [t for _, tasks in phases for t in tasks]
    return {
        **_unstarted(id), "ready_tasks": ready, "lane_model": "phase", "repo": "~/src/app", "repo_dir": "app",
        "phases": [{"phase": p, "ready_tasks": tasks, "surfaces": []} for p, tasks in phases],
    }


def _stored(phase: str, *, state="ready", requires=("docker", "repo:app"), seq=0, outcome="", epoch=0, initiative="ship"):
    return {"initiative": initiative, "phase": phase, "state": state, "requires_json": list(requires), "band": 1,
            "seq": seq, "repo": "~/src/app", "outcome": outcome, "claim_epoch": epoch}


def test_a_phase_requires_its_tasks_tokens_its_repo_and_docker_while_the_gate_is_on():
    assert chair_phase_queue.requirements(["os:macos", "os:macos"], "app", True) == ["docker", "os:macos", "repo:app"]
    assert chair_phase_queue.requirements([], "", False) == []


def test_only_phase_initiatives_get_rows_in_docket_order():
    initiatives = [
        _unstarted("old"),
        _phase_initiative("ship", ("p1", [_task("t1", "p1", ["os:macos"])]), ("p2", [_task("t2", "p2")])),
        _phase_initiative("next", ("p1", [_task("n1", "p1")])),
    ]
    rows = chair_phase_queue.wanted(initiatives, lane_prereqs=False)
    assert [(r["initiative"], r["phase"], r["seq"], r["requires"]) for r in rows] == [
        ("ship", "p1", 0, ["os:macos", "repo:app"]), ("ship", "p2", 1, ["repo:app"]), ("next", "p1", 1000, ["repo:app"]),
    ]
    assert {r["repo"] for r in rows} == {"~/src/app"}


def test_a_new_phase_is_inserted_and_a_stored_one_left_alone():
    want = chair_phase_queue.wanted([_phase_initiative("ship", ("p1", [_task("t1", "p1")]))], True)
    assert [c["op"] for c in chair_phase_queue.changes(want, [])] == ["insert"]
    assert chair_phase_queue.changes(want, [_stored("p1")]) == []


def test_a_ready_row_whose_order_or_requirements_moved_is_updated_and_a_claimed_one_never():
    want = chair_phase_queue.wanted([_phase_initiative("ship", ("p1", [_task("t1", "p1", ["os:macos"])]))], True)
    assert [c["op"] for c in chair_phase_queue.changes(want, [_stored("p1", seq=4)])] == ["update"]
    assert chair_phase_queue.changes(want, [_stored("p1", state="claimed", seq=4)]) == []


def test_a_finished_phase_ready_again_is_reopened_at_the_epoch_seen_unless_it_ran_out_of_attempts():
    want = chair_phase_queue.wanted([_phase_initiative("ship", ("p1", [_task("t1", "p1")]))], True)
    (reopen,) = chair_phase_queue.changes(want, [_stored("p1", state="finished", outcome="quarantined", epoch=3)])
    assert (reopen["op"], reopen["epoch"]) == ("reopen", 3)
    assert chair_phase_queue.changes(want, [_stored("p1", state="finished", outcome="failed", epoch=3)]) == []


def test_the_tick_plans_one_queue_action_only_while_the_store_lacks_a_row():
    initiatives = [_phase_initiative("ship", ("p1", [_task("t1", "p1")])), _unstarted("b")]
    queued = plan_tick({**_tick_facts(initiatives=initiatives), "phase_queue": {"rows": [], "lane_prereqs": True}}, NOW)
    (action,) = [a for a in queued if a["kind"] == "queue_phases"]
    assert [(c["op"], c["initiative"], c["phase"], c["requires"]) for c in action["changes"]] == [
        ("insert", "ship", "p1", ["docker", "repo:app"]),
    ]
    assert [a["initiative"] for a in queued if a["kind"] == "launch_epic"] == ["b"]
    held = {"rows": [_stored("p1")], "lane_prereqs": True}
    assert not [a for a in plan_tick({**_tick_facts(initiatives=initiatives), "phase_queue": held}, NOW) if a["kind"] == "queue_phases"]
    assert not [a for a in plan_tick(_tick_facts(initiatives=initiatives), NOW) if a["kind"] == "queue_phases"]


def test_the_idle_count_leaves_out_a_phase_initiative_a_worker_is_running():
    """The docket entry of an initiative with a live run is passed too (`gather_facts` passes the whole docket), so a
    worker holding one phase does not make the initiative's other ready tasks read as idle work."""
    live = _phase_initiative("ship", ("p2", [_task("t2", "p2"), _task("t3", "p2")]))
    inputs = {"free_lanes": 2, "ready": 3, "queued": 0, "open_signature": None}
    assert less_phase_ready(inputs, [live, _unstarted("b")])["ready"] == 1
