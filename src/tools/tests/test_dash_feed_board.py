import json
from pathlib import Path

import pytest
from test_dash_feed import _assert_matches_fixture_keys_and_nulls, _fixture_inputs
from test_dash_feed_tasks_contract import _row, _store

from agent_tools import console_screen, dash_feed, run_store

BOARD_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "dash_feed_board_v1.json").read_text(encoding="utf-8"))
SLIM_BOARD = json.loads((Path(__file__).parent / "fixtures" / "dash_feed_board_slim_v1.json").read_text(encoding="utf-8"))["board"]
NOW = "2026-09-29T00:00:00Z"
STATUSES = ("ready", "building", "approved", "merged", "landed", "quarantined", "dropped", "blocked")

ROWS = [
    {"kind": "initiative", "initiative": "alpha", "extra": {"epic": "e1"}},
    {"kind": "initiative", "initiative": "beta", "extra": {}},
    _row("a1", "done", "p1", initiative="alpha"),
    _row("a2", "ready", "p1", initiative="alpha"),
    _row("a3", "ready", "p2", needs=["a2"], initiative="alpha"),
    _row("b1", "quarantined", "q1", initiative="beta"),
    _row("b2", "in_progress", "q1", initiative="beta"),
]
LANES = [
    console_screen.LaneRow("alpha-1", "host-a", "", "p1", "build", 1, 4, 0.75, 0, 2),
    console_screen.LaneRow("beta-1", "host-b", "", "q1", "build", 1, 5, 1.125, 0, 1),
]
CALLS = {
    "alpha-1": [
        {"role": "build", "task_id": "a1", "cost_usd": 0.25, "ts": "2026-09-28T23:50:00Z"},
        {"role": "build", "task_id": "a2", "cost_usd": 0.5, "ts": "2026-09-28T23:55:00Z"},
    ],
    "beta-1": [
        {"role": "build", "task_id": "b1", "cost_usd": 0.125, "ts": "2026-09-28T23:58:00Z"},
        {"role": "build", "task_id": "b2", "cost_usd": 1.0, "ts": "2026-09-28T23:59:00Z"},
    ],
}
BEATS = {
    "host-a": {"host_facts": {"cpu_count": 8, "mem_total_gb": 32}, "host_load": {"cpu_pct": 12.5, "load_avg_1m": 4.0, "mem_used_gb": 10.256}},
    "host-b": {"host_facts": {"cpu_count": 4, "mem_total_gb": 16}, "host_load": {"cpu_pct": 80.0, "load_avg_1m": 6.0, "mem_used_gb": 8.0}},
}


def _gather(monkeypatch, root):
    _store(root, [("alpha-1", "a1"), ("alpha-1", "a2"), ("beta-1", "b1"), ("beta-1", "b2")])
    for run, calls in CALLS.items():
        (root / f"{run}.usage.json").write_text(json.dumps({"calls": calls}), encoding="utf-8")
    hosts = [
        {"name": name, "state": "active", "capacity": 3, "in_use": 1, "beat_at": NOW, "versions_json": json.dumps(beat)}
        for name, beat in BEATS.items()
    ]

    def gather(runs_dir, work_dir, now, local_name, local_capacity, spend, local_versions=None):
        return {"hosts": hosts, "lanes": LANES, "chair": [{"holder": "chair@host-b:1"}], "spend": spend}

    monkeypatch.setattr(dash_feed, "_spend", lambda runs_dir, now: {"five_hour_fraction": 0.1})
    monkeypatch.setattr(dash_feed, "_activity", lambda *args: {"chair_agents": [], "spend_split": None, "spend_sources": None})
    monkeypatch.setattr(dash_feed, "_local_identity", lambda runs_dir, profile: ("host-b", 3))
    monkeypatch.setattr(dash_feed, "_local_login", lambda local_name: None)
    monkeypatch.setattr(dash_feed.console_screen, "gather", gather)
    monkeypatch.setattr(dash_feed.run_store, "read_queue", lambda runs_dir, initiative=None, kind=None: [r for r in ROWS if kind in (None, r["kind"])])
    monkeypatch.setattr(dash_feed.run_store, "initiative_short_ids", lambda runs_dir: {"alpha": "I1", "beta": "I2"})
    monkeypatch.setattr(dash_feed.courier, "inbox", lambda blob, label=None, holder=None: [])
    return json.loads(json.dumps(dash_feed.gather_feed(root, root, NOW)))


@pytest.fixture
def feed(monkeypatch, tmp_path):
    return _gather(monkeypatch, tmp_path)


def _counts(**given):
    return {status: given.get(status, 0) for status in STATUSES}


def _flat(board):
    epics = board["epics"]
    initiatives = [i for e in epics for i in e["initiatives"]]
    phases = [p for i in initiatives for p in i["phases"]]
    return epics, initiatives, phases, [t for p in phases for t in p["tasks"]]


def test_the_board_nests_epic_initiative_phase_and_task_with_the_no_epic_group_last(feed):
    epics, initiatives, phases, tasks = _flat(feed["board"])

    assert [(e["id"], e["title"], e["status"]) for e in epics] == [("e1", "e1", "building"), ("", "no epic", "building")]
    assert [(i["id"], i["short_id"]) for i in initiatives] == [("alpha", "I1"), ("beta", "I2")]
    assert [(p["id"], p["status"]) for p in phases] == [("p1", "building"), ("p2", "blocked"), ("q1", "building")]
    assert [(t["id"], t["status"], t["run"], t["host"], t["cost_usd"]) for t in tasks] == [
        ("a1", "landed", "alpha-1", "host-a", 0.25),
        ("a2", "building", "alpha-1", "host-a", 0.5),
        ("a3", "blocked", "", "", 0.0),
        ("b1", "quarantined", "beta-1", "host-b", 0.125),
        ("b2", "building", "beta-1", "host-b", 1.0),
    ]


def test_a_phase_costs_its_tasks_and_an_initiative_its_phases(feed):
    epics, initiatives, phases, _ = _flat(feed["board"])

    for phase in phases:
        assert phase["cost_usd"] == round(sum(t["cost_usd"] for t in phase["tasks"]), 4)
    for initiative in initiatives:
        assert initiative["cost_usd"] == round(sum(p["cost_usd"] for p in initiative["phases"]), 4)
    assert [p["cost_usd"] for p in phases] == [0.75, 0.0, 1.125]
    assert [i["cost_usd"] for i in initiatives] == [0.75, 1.125]
    assert [e["cost_usd"] for e in epics] == [0.75, 1.125]


def test_every_level_counts_the_tasks_under_it_by_status(feed):
    epics, initiatives, phases, _ = _flat(feed["board"])

    assert [p["counts"] for p in phases] == [
        _counts(landed=1, building=1), _counts(blocked=1), _counts(quarantined=1, building=1),
    ]
    assert [i["counts"] for i in initiatives] == [
        _counts(landed=1, building=1, blocked=1), _counts(quarantined=1, building=1),
    ]
    assert [e["counts"] for e in epics] == [i["counts"] for i in initiatives]


def test_each_machine_lists_its_own_lanes(feed):
    by_name = {m["name"]: m for m in feed["machines"]}

    assert by_name["host-a"]["lanes"] == [
        {"run": "alpha-1", "initiative": "alpha", "phase": "p1", "tasks": ["a2"], "elapsed_s": 600, "cost_usd": 0.75},
    ]
    assert by_name["host-b"]["lanes"] == [
        {"run": "beta-1", "initiative": "beta", "phase": "q1", "tasks": ["b2"], "elapsed_s": 120, "cost_usd": 1.125},
    ]


def test_each_machine_carries_the_load_its_beat_reports(feed):
    by_name = {m["name"]: m["load"] for m in feed["machines"]}

    assert by_name["host-a"] == {"cpu_pct": 12.5, "load_avg_per_core": 0.5, "mem_used_gb": 10.26, "mem_total_gb": 32.0}
    assert by_name["host-b"] == {"cpu_pct": 80.0, "load_avg_per_core": 1.5, "mem_used_gb": 8.0, "mem_total_gb": 16.0}


def test_a_store_error_gives_an_empty_board_and_empty_lanes_and_the_runs_stay(monkeypatch, tmp_path):
    def broken(runs_dir, run_ids):
        raise OSError("store gone")

    monkeypatch.setattr(run_store, "attempt_causes_for", broken)
    feed = _gather(monkeypatch, tmp_path)

    assert feed["board"] == {"epics": []}
    assert [m["lanes"] for m in feed["machines"]] == [[], []]
    assert [r["run"] for r in feed["runs"]][:2] == ["alpha-1", "beta-1"]


def test_a_code_fault_is_not_swallowed(monkeypatch, tmp_path):
    def faulty(runs_dir, run_ids):
        raise KeyError("a bug")

    monkeypatch.setattr(run_store, "attempt_causes_for", faulty)
    with pytest.raises(KeyError):
        _gather(monkeypatch, tmp_path)


def test_snapshot_without_a_board_reads_the_empty_board():
    assert dash_feed.snapshot(NOW, {}, {}, [], [], [], 0, [], 0, [])["board"] == {"epics": []}


def _levels(board):
    epic = board["epics"][0]
    initiative = epic["initiatives"][0]
    phase = initiative["phases"][0]
    return [list(epic), list(initiative), list(phase), list(phase["tasks"][0]), list(epic["counts"])]


def test_the_new_keys_of_a_fixture_built_feed_are_the_board_fixture_s(feed):
    args, kwargs = _fixture_inputs()
    built = dash_feed.snapshot(*args, **kwargs)
    machine, expected = built["machines"][0], BOARD_FIXTURE["machines"][0]

    # The built board is the v1 fixture's two epics; the slim fixture has a third, so pair epics by id.
    slim_by_id = {e["id"]: e for e in SLIM_BOARD["epics"]}
    _assert_matches_fixture_keys_and_nulls(built["board"], {"epics": [slim_by_id[e["id"]] for e in built["board"]["epics"]]})
    # The epics with no counterpart in the built board are named, and every slim epic has the built epics' keys.
    assert slim_by_id.keys() - {e["id"] for e in built["board"]["epics"]} == {"ops-cleanup"}
    assert all(list(e) == list(built["board"]["epics"][0]) for e in SLIM_BOARD["epics"])
    _assert_matches_fixture_keys_and_nulls({k: machine[k] for k in ("lanes", "load")}, {k: expected[k] for k in ("lanes", "load")})
    assert machine.keys() == expected.keys()
    # The live gather has the same key structure at every level and in every lane and load.
    assert _levels(feed["board"]) == _levels(SLIM_BOARD)
    live = feed["machines"][0]
    assert live.keys() == expected.keys()
    assert list(live["lanes"][0]) == list(expected["lanes"][0]) and list(live["load"]) == list(expected["load"])
