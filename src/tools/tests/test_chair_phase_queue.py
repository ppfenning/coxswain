"""The chair's side of the phase queue, pure: which rows it wants, what it writes, the tick action that carries it, and
the rule that keeps every old-model action off a phase initiative."""

from datetime import UTC, datetime

from agent_tools import chair_phase_queue
from agent_tools.chair_plan import plan_tick
from agent_tools.chair_plan_fill import less_phase_ready
from agent_tools.chair_read_phase_queue import initiative_sets
from tests.test_chair_plan import _facts as _tick_facts
from tests.test_chair_plan import _initiative, _unstarted

NOW = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
STAMP = "2026-11-20T10:00:00Z"


def _task(id: str, phase: str, requires=()) -> dict:
    return {"id": id, "phase": phase, "needs": [], "requires": list(requires)}


def _phase_initiative(id: str = "ship", *phases: tuple[str, list[dict]], started: bool = False) -> dict:
    ready = [t for _, tasks in phases for t in tasks]
    return {
        **_unstarted(id), "started": started, "ready_tasks": ready, "lane_model": "phase", "repo": "~/src/app",
        "repo_dir": "app", "phases": [{"phase": p, "ready_tasks": tasks, "surfaces": []} for p, tasks in phases],
    }


def _stored(phase: str, *, state="ready", requires=("docker", "repo:app"), seq=0, outcome="", epoch=0, initiative="ship",
            attempts=0, lease_until=""):
    return {"initiative": initiative, "phase": phase, "state": state, "requires_json": list(requires), "band": 1,
            "seq": seq, "repo": "~/src/app", "outcome": outcome, "claim_epoch": epoch, "attempts": attempts,
            "lease_until": lease_until}


def _queue(rows=(), phase_initiatives=(), closed=(), readable=True) -> dict:
    return {"rows": list(rows), "readable": readable, "lane_prereqs": True, "phase_initiatives": list(phase_initiatives),
            "closed": list(closed)}


def _ops(changes) -> list[tuple[str, str, str]]:
    return [(c["op"], c["initiative"], c["phase"]) for c in changes]


def test_a_phase_requires_its_tasks_tokens_its_repo_and_docker_while_the_gate_is_on():
    assert chair_phase_queue.requirements(["os:macos", "os:macos"], "app", True) == ["docker", "os:macos", "repo:app"]
    assert chair_phase_queue.requirements([], "", False) == []


def test_only_phase_initiatives_get_rows_in_docket_order():
    initiatives = [
        _unstarted("old"),
        _phase_initiative("ship", ("p1", [_task("t1", "p1", ["os:macos"])]), ("p2", [_task("t2", "p2")])),
        _phase_initiative("next", ("p1", [_task("n1", "p1")])),
    ]
    rows = chair_phase_queue.rows(initiatives, lane_prereqs=False)
    assert [(r["initiative"], r["phase"], r["seq"], r["requires"]) for r in rows] == [
        ("ship", "p1", 0, ["os:macos", "repo:app"]), ("ship", "p2", 1, ["repo:app"]), ("next", "p1", 1000, ["repo:app"]),
    ]


def test_a_held_closed_or_stopped_phase_is_not_wanted():
    initiatives = [_phase_initiative("ship", ("p1", [_task("t1", "p1")]), ("p2", [_task("t2", "p2")]))]
    assert chair_phase_queue.rows(initiatives, True, holds={"ship"}) == []
    assert chair_phase_queue.rows(initiatives, True, closed={"ship"}) == []
    assert chair_phase_queue.rows(initiatives, True, stopped=True) == []
    assert [r["phase"] for r in chair_phase_queue.rows(initiatives, True, held_phases={("ship", "p1")})] == ["p2"]


def test_a_new_phase_is_inserted_and_a_stored_one_left_alone():
    want = chair_phase_queue.rows([_phase_initiative("ship", ("p1", [_task("t1", "p1")]))], True)
    assert _ops(chair_phase_queue.changes(want, [], STAMP)) == [("insert", "ship", "p1")]
    assert chair_phase_queue.changes(want, [_stored("p1")], STAMP) == []


def test_a_ready_row_whose_order_or_requirements_moved_is_updated_and_a_claimed_one_never():
    want = chair_phase_queue.rows([_phase_initiative("ship", ("p1", [_task("t1", "p1", ["os:macos"])]))], True)
    assert _ops(chair_phase_queue.changes(want, [_stored("p1", seq=4)], STAMP)) == [("update", "ship", "p1")]
    held = _stored("p1", state="claimed", seq=4, attempts=1, lease_until="2026-11-20T10:00:30Z")
    assert chair_phase_queue.changes(want, [held], STAMP) == []


def test_a_held_phase_s_ready_row_is_withdrawn_and_comes_back_when_it_is_wanted_again():
    stored = [_stored("p1"), _stored("p2", state="claimed", lease_until="2026-11-20T10:00:30Z", attempts=1)]
    withdrawn = chair_phase_queue.changes([], stored, STAMP, holds={"ship"})
    assert [(c["op"], c["phase"], c["reason"]) for c in withdrawn] == [("withdraw", "p1", "its initiative is held")]
    assert _ops(chair_phase_queue.changes([], [_stored("p1")], STAMP, closed={"ship"})) == [("withdraw", "ship", "p1")]
    assert _ops(chair_phase_queue.changes([], [_stored("p1")], STAMP, stopped=True)) == [("withdraw", "ship", "p1")]
    assert chair_phase_queue.changes([], [_stored("p1")], STAMP) == []  # not wanted, but nothing withholds it
    want = chair_phase_queue.rows([_phase_initiative("ship", ("p1", [_task("t1", "p1")]))], True)
    (back,) = chair_phase_queue.changes(want, [_stored("p1", state="cancelled", epoch=2)], STAMP)
    assert (back["op"], back["epoch"]) == ("reopen", 2)


def test_a_finished_phase_ready_again_is_reopened_unless_it_ran_out_of_attempts():
    want = chair_phase_queue.rows([_phase_initiative("ship", ("p1", [_task("t1", "p1")]))], True)
    (reopen,) = chair_phase_queue.changes(want, [_stored("p1", state="finished", outcome="quarantined", epoch=3)], STAMP)
    assert (reopen["op"], reopen["epoch"]) == ("reopen", 3)
    assert chair_phase_queue.changes(want, [_stored("p1", state="finished", outcome="failed", epoch=3)], STAMP) == []


def test_a_phase_whose_claims_ran_out_is_finished_failed_and_never_reopened():
    want = chair_phase_queue.rows([_phase_initiative("ship", ("p1", [_task("t1", "p1")]))], True)
    lapsed = _stored("p1", state="claimed", attempts=3, epoch=3, lease_until="2026-11-20T09:00:00Z")
    (change,) = chair_phase_queue.changes(want, [lapsed], STAMP)
    assert (change["op"], change["phase"]) == ("exhaust", "p1")
    live = {**lapsed, "lease_until": "2026-11-20T10:00:30Z"}
    assert chair_phase_queue.changes(want, [live], STAMP) == []  # its holder may still finish it


def test_the_tick_plans_one_queue_action_only_while_the_store_lacks_a_row():
    initiatives = [_phase_initiative("ship", ("p1", [_task("t1", "p1")])), _unstarted("b")]
    queued = plan_tick({**_tick_facts(initiatives=initiatives), "phase_queue": _queue()}, NOW)
    (action,) = [a for a in queued if a["kind"] == "queue_phases"]
    assert [(c["op"], c["initiative"], c["phase"], c["requires"]) for c in action["changes"]] == [
        ("insert", "ship", "p1", ["docker", "repo:app"]),
    ]
    assert [a["initiative"] for a in queued if a["kind"] == "launch_epic"] == ["b"]
    held = _queue([_stored("p1")])
    assert not [a for a in plan_tick({**_tick_facts(initiatives=initiatives), "phase_queue": held}, NOW) if a["kind"] == "queue_phases"]
    assert not [a for a in plan_tick(_tick_facts(initiatives=initiatives), NOW) if a["kind"] == "queue_phases"]


def test_the_tick_withdraws_a_held_initiative_s_phase():
    initiatives = [_phase_initiative("ship", ("p1", [_task("t1", "p1")]))]
    facts = {**_tick_facts(initiatives=initiatives, held_initiatives=["ship"]), "phase_queue": _queue([_stored("p1")])}
    (action,) = [a for a in plan_tick(facts, NOW) if a["kind"] == "queue_phases"]
    assert _ops(action["changes"]) == [("withdraw", "ship", "p1")]


def test_no_old_model_action_is_planned_for_a_phase_initiative():
    """A started phase initiative with ready work, and quarantines in it and in a phase initiative the docket does not
    list: the old model would clear its branches and relaunch it, retry one task and rescue another. None of that is
    planned; an old-model initiative beside them still gets each."""
    phase = _phase_initiative("ship", ("p1", [_task("t1", "p1")]), started=True)
    quarantines = [
        {"task_id": "q1", "initiative": "gone", "cause": "harness", "harness_failures": 1, "has_patch": False, "rescue_failed": False},
        {"task_id": "q2", "initiative": "ship", "cause": "harness", "harness_failures": 1, "has_patch": True, "rescue_failed": False},
        {"task_id": "q3", "initiative": "old", "cause": "harness", "harness_failures": 1, "has_patch": False, "rescue_failed": False},
    ]
    facts = {
        **_tick_facts(initiatives=[phase, _initiative("old")], run_exited={"ship": True, "old": True}, quarantines=quarantines),
        "phase_queue": _queue(phase_initiatives=["gone", "ship"]),
    }
    actions = plan_tick(facts, NOW)
    old_model = [a for a in actions if a["kind"] in chair_phase_queue.OLD_MODEL_KINDS]
    assert {a.get("initiative") for a in old_model} == {"old"}, actions
    assert any(a["kind"] == "queue_phases" for a in actions)
    unguarded = plan_tick({**facts, "phase_queue": _queue(phase_initiatives=[])}, NOW)
    assert {"gone"} <= {a.get("initiative") for a in unguarded if a["kind"] in chair_phase_queue.OLD_MODEL_KINDS}


def test_the_phase_model_is_read_from_the_docket_and_from_the_store_rows():
    facts = {**_tick_facts(initiatives=[_phase_initiative("ship"), _unstarted("b")]), "phase_queue": _queue(phase_initiatives=["gone"])}
    assert chair_phase_queue.phase_model_ids(facts) == {"ship", "gone"}
    actions = [{"kind": "relaunch", "initiative": "ship"}, {"kind": "relaunch", "initiative": "b"}, {"kind": "unlaunched", "initiative": "ship"}]
    assert chair_phase_queue.withhold_old_model(actions, {"ship"}) == actions[1:]


def test_initiative_rows_name_the_phase_ones_and_the_closed_ones():
    heads = [
        {"kind": "initiative", "initiative": "a", "state": "todo", "extra": {"lane_model": "phase"}},
        {"kind": "initiative", "initiative": "b", "state": "done", "extra": {}},
        {"kind": "initiative", "initiative": "c", "state": "dropped", "extra": {"lane_model": "phase"}},
    ]
    assert initiative_sets(heads) == (["a", "c"], ["b", "c"])
    assert initiative_sets([{"kind": "task", "initiative": "a", "state": "done", "extra": {"lane_model": "phase"}}]) == ([], [])


def test_an_unreadable_queue_plans_no_write_and_still_keeps_the_old_model_off_its_initiatives():
    """The queue table could not be read: no queue change. The phase model is known from the tick's own work-item
    rows, so a phase initiative still gets no relaunch."""
    phase = _phase_initiative("ship", ("p1", [_task("t1", "p1")]), started=True)
    facts = {
        **_tick_facts(initiatives=[phase], run_exited={"ship": True}),
        "phase_queue": _queue(phase_initiatives=["ship"], readable=False),
    }
    actions = plan_tick(facts, NOW)
    assert not [a for a in actions if a["kind"] == "queue_phases"]
    assert not [a for a in actions if a["kind"] in chair_phase_queue.OLD_MODEL_KINDS]


def test_a_reopened_phase_with_three_spent_claims_is_reopened_not_exhausted():
    want = chair_phase_queue.rows([_phase_initiative("ship", ("p1", [_task("t1", "p1")]))], True)
    done = _stored("p1", state="finished", outcome="approved", epoch=3, attempts=3)
    assert _ops(chair_phase_queue.changes(want, [done], STAMP)) == [("reopen", "ship", "p1")]


def test_the_idle_count_leaves_out_a_phase_initiative_a_worker_is_running():
    """The docket entry of an initiative with a live run is passed too (`gather_facts` passes the whole docket), so a
    worker holding one phase does not make the initiative's other ready tasks read as idle work."""
    live = _phase_initiative("ship", ("p2", [_task("t2", "p2"), _task("t3", "p2")]))
    inputs = {"free_lanes": 2, "ready": 3, "queued": 0, "open_signature": None}
    assert less_phase_ready(inputs, [live, _unstarted("b")])["ready"] == 1


def test_a_phase_finished_approved_approves_its_open_tasks_at_their_roll_up_commits():
    stored = [_stored("p1", state="finished", outcome="approved"), _stored("p2", state="finished", outcome="quarantined")]
    tasks = [{"initiative": "ship", "task_id": t, "phase": p, "state": s}
             for t, p, s in (("t2", "p1", "ready"), ("t1", "p1", "approved"), ("t3", "p1", "ready"), ("t4", "p2", "ready"))]
    rollups = [{"initiative": "ship", "task_id": "t2", "subject_sha": "old"}, {"initiative": "ship", "task_id": "t2", "subject_sha": "b2"},
               {"initiative": "ship", "task_id": "t1", "subject_sha": "a1"}, {"initiative": "ship", "task_id": "t4", "subject_sha": "d4"}]
    # t1 is approved already, t3 has no commit on the branch, and p2's review quarantined it: only t2 moves, at its last
    assert chair_phase_queue.approvals(stored, tasks, rollups) == [
        {"initiative": "ship", "phase": "p1", "task_id": "t2", "commit": "b2"},
    ]


def test_the_tick_approves_an_approved_phases_tasks_and_does_not_reopen_it_meanwhile():
    initiatives = [_phase_initiative("ship", ("p1", [_task("t1", "p1")]))]
    queue = {**_queue([_stored("p1", state="finished", outcome="approved")]),
             "tasks": [{"initiative": "ship", "task_id": "t1", "phase": "p1", "state": "ready"}],
             "rollups": [{"initiative": "ship", "task_id": "t1", "subject_sha": "a1"}]}
    planned = plan_tick({**_tick_facts(initiatives=initiatives), "phase_queue": queue}, NOW)
    (approve,) = [a for a in planned if a["kind"] == "approve_phase_tasks"]
    assert approve["approvals"] == [{"initiative": "ship", "phase": "p1", "task_id": "t1", "commit": "a1"}]
    assert not [a for a in planned if a["kind"] == "queue_phases"]
    settled = {**queue, "tasks": [{"initiative": "ship", "task_id": "t1", "phase": "p1", "state": "approved"}]}
    again = plan_tick({**_tick_facts(initiatives=initiatives), "phase_queue": settled}, NOW)
    assert not [a for a in again if a["kind"] == "approve_phase_tasks"]
