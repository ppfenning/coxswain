import json

import pytest

from agent_tools import dash_feed, runs_stranded
from agent_tools.chair_read_idle_backlog import blocked_ready


def _task(initiative, phase, task_id, state, needs=()):
    return {
        "kind": "task", "initiative": initiative, "phase": phase, "task_id": task_id, "state": state,
        "needs": list(needs), "extra": {},
    }


# alpha covers landed, approved, dropped, blocked (its need is only approved), ready, and a task the store still
# calls ready although an approved record waits to land (a stranded task).
# beta covers running, quarantined, a dead build (in_progress, no lane in its phase), a hold and a todo.
STORE = [
    _task("alpha", "p1", "a-landed", "done"),
    _task("alpha", "p1", "a-approved", "approved"),
    _task("alpha", "p1", "a-dropped", "dropped"),
    _task("alpha", "p2", "a-blocked", "ready", needs=["a-approved"]),
    _task("alpha", "p2", "a-ready", "ready", needs=["a-landed", "a-dropped"]),
    _task("alpha", "p2", "a-stranded", "ready"),
    _task("beta", "p1", "b-running", "in_progress"),
    _task("beta", "p1", "b-quarantined", "quarantined"),
    _task("beta", "p2", "b-dead", "in_progress"),
    _task("beta", "p2", "b-held", "blocked"),
    _task("beta", "p2", "b-todo", "todo"),
    _task("beta", "p2", "b-behind-todo", "ready", needs=["b-todo"]),
]
RECORDS = [
    {"run": "alpha-1", "task": "a-approved", "initiative": "alpha", "phase": "p1", "arbitration": {"verdict": "approve"}},
    {"run": "alpha-1", "task": "a-stranded", "initiative": "alpha", "phase": "p2", "arbitration": {"verdict": "approve"}},
]
LIVE = {("beta", "p1")}


@pytest.fixture
def queue(monkeypatch, tmp_path):
    monkeypatch.setattr(dash_feed.run_store, "read_queue", lambda runs_dir: STORE)
    monkeypatch.setattr(dash_feed, "_task_records", lambda runs_dir: RECORDS)
    return dash_feed._queue(tmp_path, LIVE)[0]


def _states(queue, initiative):
    row = next(row for row in queue if row["initiative"] == initiative)
    return {task["id"]: task["state"] for phase in row["phases"] for task in phase["tasks"]}


def test_each_row_lists_phases_in_order_with_every_task_state(queue):
    assert {row["initiative"]: row["phases"] for row in queue} == {
        "alpha": [
            {"id": "p1", "tasks": [
                {"id": "a-landed", "state": "landed"},
                {"id": "a-approved", "state": "approved"},
                {"id": "a-dropped", "state": "dropped"},
            ]},
            {"id": "p2", "tasks": [
                {"id": "a-blocked", "state": "blocked"},
                {"id": "a-ready", "state": "ready"},
                {"id": "a-stranded", "state": "approved"},
            ]},
        ],
        "beta": [
            {"id": "p1", "tasks": [
                {"id": "b-running", "state": "running"},
                {"id": "b-quarantined", "state": "quarantined"},
            ]},
            {"id": "p2", "tasks": [
                {"id": "b-dead", "state": "blocked"},
                {"id": "b-held", "state": "blocked"},
                {"id": "b-behind-todo", "state": "blocked"},
            ]},
        ],
    }


def test_the_phase_counts_are_what_they_were(queue):
    assert [(r["initiative"], r["phases_landed"], r["phases_total"], r["current_phase"]) for r in queue] == [
        ("alpha", 0, 2, "p1"),
        ("beta", 0, 2, "p1"),
    ]


@pytest.mark.parametrize(
    ("live", "state"),
    [
        (set(), "blocked"),
        ({("alpha", "p1")}, "blocked"),
        ({("beta", "p2")}, "blocked"),
        ({("beta", "p1")}, "running"),
        ({("beta", "")}, "running"),
    ],
)
def test_a_build_is_running_only_while_a_live_lane_covers_its_initiative_and_phase(live, state):
    phases = dash_feed._initiative_phases(STORE, "beta", live, set())

    assert {t["id"]: t["state"] for t in phases[0]["tasks"]}["b-running"] == state


def test_live_phases_come_from_the_lane_run_key_and_phase():
    lanes = [
        dash_feed.console_screen.LaneRow("beta-2", None, "", "p1", "build", 1, 3, 0.5, 0, 2),
        dash_feed.console_screen.LaneRow("alpha-10", "host-a", "", "", "starting", 0, 0, 0.0, 0, 1),
    ]

    assert dash_feed._live_phases(lanes) == {("beta", "p1"), ("alpha", "")}


def test_the_approved_tasks_are_the_ones_runs_stranded_lists(queue):
    items = [
        {"id": row["task_id"], "initiative": row["initiative"], "phase": row["phase"], "state": row["state"], "repo": "/r"}
        for row in STORE
    ]
    stranded = {row["task"] for row in runs_stranded.stranded(RECORDS, items)}
    approved = {task for state in (_states(queue, "alpha"), _states(queue, "beta")) for task, s in state.items() if s == "approved"}

    assert stranded == approved == {"a-approved", "a-stranded"}


def test_a_blocked_task_waits_on_the_same_needs_blocked_ready_names(queue):
    items = [{"id": r["task_id"], "initiative": r["initiative"], "state": r["state"], "needs": r["needs"]} for r in STORE]
    states = {**_states(queue, "alpha"), **_states(queue, "beta")}
    landed = {i["id"] for i in items if i["state"] in ("done", "dropped")}
    waiting = {i["id"]: [n for n in i["needs"] if n not in landed] for i in items if i["state"] == "ready"}

    assert [row["task"] for row in blocked_ready(items)] == ["a-blocked"]
    assert {row["task"]: row["unlanded_needs"] for row in blocked_ready(items)} == {"a-blocked": waiting["a-blocked"]}
    assert {task: states[task] == "blocked" for task, unmet in waiting.items() if task != "a-stranded"} == {
        task: bool(unmet) for task, unmet in waiting.items() if task != "a-stranded"
    }


@pytest.mark.parametrize(
    ("task", "on_live_lane", "stranded", "state"),
    [
        (_task("i", "p", "t", "done"), False, False, "landed"),
        (_task("i", "p", "t", "dropped"), False, False, "dropped"),
        (_task("i", "p", "t", "approved"), False, False, "approved"),
        (_task("i", "p", "t", "quarantined"), False, False, "quarantined"),
        (_task("i", "p", "t", "in_progress"), True, False, "running"),
        (_task("i", "p", "t", "in_progress"), False, False, "blocked"),
        (_task("i", "p", "t", "ready"), False, False, "ready"),
        (_task("i", "p", "t", "ready", needs=["n"]), False, False, "blocked"),
        (_task("i", "p", "t", "ready", needs=["n"]), False, True, "approved"),
        (_task("i", "p", "t", "blocked"), False, False, "blocked"),
        (_task("i", "p", "t", "todo"), False, False, None),
        (_task("i", "p", "t", "mystery"), False, False, None),
    ],
)
def test_task_state_rules(task, on_live_lane, stranded, state):
    assert dash_feed._task_state(task, set(), on_live_lane, stranded) == state


def test_task_records_default_run_task_and_phase_from_the_path_and_skip_a_bad_file(tmp_path):
    folder = tmp_path / "alpha-1" / "tasks" / "p1"
    folder.mkdir(parents=True)
    (folder / "t1.json").write_text(json.dumps({"initiative": "alpha"}))
    (folder / "t2.json").write_text("{not json")

    assert dash_feed._task_records(tmp_path) == [{"run": "alpha-1", "task": "t1", "phase": "p1", "initiative": "alpha"}]
