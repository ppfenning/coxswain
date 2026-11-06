import pytest

from agent_tools.dash_board import STATUSES, build_board, rollup_status, task_costs

ROWS = [
    {"initiative": "alpha", "phase": "p2", "task_id": "t-build"},
    {"initiative": "alpha", "phase": "p2", "task_id": "t-ready"},
    {"initiative": "alpha", "phase": "p1", "task_id": "t-landed"},
    {"initiative": "alpha", "phase": "p1", "task_id": "t-merged"},
    {"initiative": "alpha", "phase": "p1", "task_id": "t-unlisted"},
    {"initiative": "beta", "phase": "p1", "task_id": "t-quar"},
    {"initiative": "beta", "phase": "p1", "task_id": "t-drop"},
    {"initiative": "beta", "phase": "p1", "task_id": "t-appr"},
    {"initiative": "beta", "phase": "p2", "task_id": "t-blk"},
    {"initiative": "beta", "phase": "p2", "task_id": "t-drop2"},
]
STATES = {
    ("alpha", "p2", "t-build"): "building",
    ("alpha", "p2", "t-ready"): "ready",
    ("alpha", "p1", "t-landed"): "landed",
    ("alpha", "p1", "t-merged"): "merged",
    ("beta", "p1", "t-quar"): "quarantined",
    ("beta", "p1", "t-drop"): "dropped",
    ("beta", "p1", "t-appr"): "approved",
    ("beta", "p2", "t-blk"): "blocked",
    ("beta", "p2", "t-drop2"): "dropped",
}
RUNS = {"t-build": ("alpha-2", "host-b")}
EPIC_OF = {"alpha": "e1", "beta": None}
SHORT_IDS = {"alpha": "I1", "beta": "I2"}
COSTS = {
    "t-build": 0.75, "t-landed": 0.5, "t-merged": 0.25, "t-unlisted": 4.0,
    "t-quar": 1.25, "t-drop": 0.25, "t-appr": 0.5, "t-blk": 0.125,
}


def _counts(**given):
    return {status: given.get(status, 0) for status in STATUSES}


def _board():
    return build_board(ROWS, STATES, RUNS, EPIC_OF, SHORT_IDS, COSTS)


def _by_id(nodes):
    return {node["id"]: node for node in nodes}


def test_task_costs_sums_matching_calls_over_every_attempt_run():
    attempts = {
        "a": [{"run_id": "r1", "task_id": "a"}, {"run_id": "r2", "task_id": "a"}, {"run_id": "r2", "task_id": "a"}],
        "b": [{"run_id": "r1", "task_id": "b"}],
    }
    calls = {
        "r1": [{"task_id": "a", "cost_usd": 0.5}, {"task_id": "b", "cost_usd": 2.0}],
        "r2": [{"task_id": "a", "cost_usd": 0.25}, {"task_id": "other", "cost_usd": 9.0}, {"task_id": "a"}],
    }
    assert task_costs(attempts, calls) == {"a": 0.75, "b": 2.0}


def test_task_costs_gives_zero_for_a_run_with_no_calls_and_rounds_to_four_places():
    attempts = {"a": [{"run_id": "r1", "task_id": "a"}, {"run_id": "gone", "task_id": "a"}]}
    calls = {"r1": [{"task_id": "a", "cost_usd": 0.1}, {"task_id": "a", "cost_usd": 0.2}, {"task_id": "a", "cost_usd": 0.00004}]}
    assert task_costs(attempts, calls) == {"a": 0.3}
    assert task_costs({"z": [{"run_id": "nope", "task_id": "z"}]}, {}) == {"z": 0.0}


def test_epics_order_by_id_with_no_epic_last_and_group_unepiced_initiatives():
    rows = [{"initiative": name, "phase": "p", "task_id": name} for name in ("n1", "b1", "a1", "n0")]
    states = {(name, "p", name): "ready" for name in ("n1", "b1", "a1", "n0")}
    epics = build_board(rows, states, {}, {"b1": "zb", "a1": "ya"}, {}, {})["epics"]
    assert [(e["id"], e["title"]) for e in epics] == [("ya", "ya"), ("zb", "zb"), ("", "no epic")]
    assert [i["id"] for i in epics[2]["initiatives"]] == ["n0", "n1"]


def test_tree_shape_and_ordering():
    epics = _board()["epics"]
    assert [e["id"] for e in epics] == ["e1", ""]
    assert epics[1]["title"] == "no epic"
    alpha, beta = epics[0]["initiatives"][0], epics[1]["initiatives"][0]
    assert (alpha["id"], alpha["short_id"], beta["id"], beta["short_id"]) == ("alpha", "I1", "beta", "I2")
    assert [p["id"] for p in alpha["phases"]] == ["p1", "p2"]
    assert [t["id"] for t in alpha["phases"][0]["tasks"]] == ["t-landed", "t-merged"]
    assert [t["id"] for t in beta["phases"][0]["tasks"]] == ["t-quar", "t-drop", "t-appr"]


def test_task_without_a_state_is_left_out_and_run_and_host_default_to_empty():
    alpha = _board()["epics"][0]["initiatives"][0]
    tasks = {t["id"]: t for phase in alpha["phases"] for t in phase["tasks"]}
    assert "t-unlisted" not in tasks
    assert tasks["t-build"] == {"id": "t-build", "status": "building", "run": "alpha-2", "host": "host-b", "cost_usd": 0.75}
    assert tasks["t-ready"] == {"id": "t-ready", "status": "ready", "run": "", "host": "", "cost_usd": 0.0}


def test_costs_sum_up_every_level():
    epic_e1, epic_none = _board()["epics"]
    alpha, beta = epic_e1["initiatives"][0], epic_none["initiatives"][0]
    assert {p["id"]: p["cost_usd"] for p in alpha["phases"]} == {"p1": 0.75, "p2": 0.75}
    assert {p["id"]: p["cost_usd"] for p in beta["phases"]} == {"p1": 2.0, "p2": 0.125}
    assert (alpha["cost_usd"], beta["cost_usd"]) == (1.5, 2.125)
    assert (epic_e1["cost_usd"], epic_none["cost_usd"]) == (1.5, 2.125)


def test_counts_hold_all_eight_statuses_at_every_level():
    epic_e1, epic_none = _board()["epics"]
    alpha, beta = epic_e1["initiatives"][0], epic_none["initiatives"][0]
    alpha_counts = _counts(ready=1, building=1, merged=1, landed=1)
    beta_counts = _counts(approved=1, quarantined=1, dropped=2, blocked=1)
    assert _by_id(alpha["phases"])["p1"]["counts"] == _counts(merged=1, landed=1)
    assert _by_id(alpha["phases"])["p2"]["counts"] == _counts(ready=1, building=1)
    assert _by_id(beta["phases"])["p1"]["counts"] == _counts(approved=1, quarantined=1, dropped=1)
    assert _by_id(beta["phases"])["p2"]["counts"] == _counts(blocked=1, dropped=1)
    assert (alpha["counts"], epic_e1["counts"]) == (alpha_counts, alpha_counts)
    assert (beta["counts"], epic_none["counts"]) == (beta_counts, beta_counts)


def test_status_rolls_up_with_dropped_children_ignored():
    epic_e1, epic_none = _board()["epics"]
    alpha, beta = epic_e1["initiatives"][0], epic_none["initiatives"][0]
    assert {p["id"]: p["status"] for p in alpha["phases"]} == {"p1": "merged", "p2": "building"}
    assert {p["id"]: p["status"] for p in beta["phases"]} == {"p1": "quarantined", "p2": "blocked"}
    assert (alpha["status"], epic_e1["status"]) == ("building", "building")
    assert (beta["status"], epic_none["status"]) == ("quarantined", "quarantined")


def test_board_inputs_are_not_mutated():
    rows, states = [dict(row) for row in ROWS], dict(STATES)
    build_board(rows, states, RUNS, EPIC_OF, SHORT_IDS, COSTS)
    assert (rows, states) == (ROWS, STATES)


def test_rollup_of_nothing_or_only_dropped_is_dropped():
    assert rollup_status([]) == "dropped"
    assert rollup_status(["dropped", "dropped"]) == "dropped"


def test_rollup_ignores_dropped_children():
    assert rollup_status(["dropped", "landed"]) == "landed"
    assert rollup_status(["dropped", "ready"]) == "ready"


@pytest.mark.parametrize(
    ("higher", "lower"),
    [
        ("building", "quarantined"),
        ("quarantined", "blocked"),
        ("blocked", "approved"),
        ("approved", "merged"),
        ("merged", "ready"),
    ],
)
def test_rollup_precedence(higher, lower):
    assert rollup_status([lower, "landed", higher]) == higher
    assert rollup_status([higher, lower]) == higher


def test_rollup_of_landed_alone_is_landed_and_ready_beats_landed():
    assert rollup_status(["landed", "landed"]) == "landed"
    assert rollup_status(["landed", "ready"]) == "ready"
