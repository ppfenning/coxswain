import json
import sqlite3
from dataclasses import replace
from typing import get_args

from test_chair_facts_phases import NOW, _deps, _write

from agent_tools import run_store
from agent_tools.chair_facts import (
    duration_records,
    gather_facts,
    phase_status,
    schedule_inputs_from_rows,
    windowed_actions,
)
from agent_tools.chair_read_record import recorder
from agent_tools.chair_read_stale import read_chair_actions
from agent_tools.chair_schedule_durations import estimate_duration, span_seconds
from agent_tools.chair_types import ActionKind

DISPATCH = {
    "max_in_flight": 2,
    "live_runs": 1,
    "hosts": [{"name": "host-a", "live_runs": 0, "capacity": 3}, {"name": "host-b", "live_runs": 2}],
}


def _t(task, phase, state, needs=(), surfaces=(), size="", initiative="init-a"):
    return {
        "id": task, "initiative": initiative, "phase": phase, "state": state,
        "needs": list(needs), "surfaces": list(surfaces), "size": size,
    }


def _inputs(items, work=(), actions=(), **kw):
    given = {"repos": {}, "land_at": {}, "live_phases": (), **kw}
    return schedule_inputs_from_rows(items, list(work), list(actions), DISPATCH, local="chair", **given)


def _phases(inputs, initiative="init-a"):
    return {p["id"]: p for i in inputs["initiatives"] if i["id"] == initiative for p in i["phases"]}


TWO_WAVES = [
    _t("t1", "p1", "ready", surfaces=["src/a.py"], size="S"),
    _t("t2", "p2", "todo", needs=["t1"], surfaces=["src/a.py", "src/b.py"], size="M"),
]


def test_two_waves_give_nodes_with_the_right_parents():
    phases = _phases(_inputs(TWO_WAVES))
    assert phases["p1"]["parents"] == [] and phases["p1"]["status"] == "ready"
    assert phases["p2"]["parents"] == ["p1"]
    assert (phases["p1"]["size"], phases["p2"]["size"]) == ("S", "M")


def test_a_shared_surface_survives_into_both_phases():
    phases = _phases(_inputs(TWO_WAVES))
    assert phases["p1"]["surfaces"] == ["src/a.py"]
    assert phases["p2"]["surfaces"] == ["src/a.py", "src/b.py"]


def test_a_later_phase_with_only_todo_tickets_is_blocked_not_running():
    phases = _phases(_inputs(TWO_WAVES))
    assert phases["p2"]["status"] == "blocked"


def test_a_phase_with_an_approved_ticket_and_no_live_run_is_not_running():
    items = [_t("t1", "p1", "approved"), _t("t2", "p1", "todo")]
    assert _phases(_inputs(items))["p1"]["status"] == "waiting"
    assert _phases(_inputs(items, live_phases=[("init-a", "p1")]))["p1"]["status"] == "running"


def test_a_quarantined_ticket_with_no_live_run_is_not_running():
    assert phase_status(["quarantined", "todo"], launchable=False, held=False) == "waiting"


def test_only_the_phase_a_live_run_names_is_running_among_in_progress_phases():
    items = [_t("t1", "p1", "in_progress"), _t("t2", "p2", "in_progress")]
    assert {p["status"] for p in _phases(_inputs(items)).values()} == {"waiting"}
    live = _phases(_inputs(items, live_phases=[("init-a", "p2")]))
    assert (live["p1"]["status"], live["p2"]["status"]) == ("waiting", "running")


def test_a_complete_phase_is_left_out_and_adds_no_parent():
    items = [_t("t1", "p1", "done"), _t("t2", "p2", "ready", needs=["t1"])]
    phases = _phases(_inputs(items))
    assert set(phases) == {"p2"} and phases["p2"]["parents"] == []


def test_an_initiative_with_nothing_open_is_left_out():
    assert _inputs([_t("t1", "p1", "done")])["initiatives"] == []


def _row(at, **action):
    """A `chair_actions` row as `read_chair_actions` returns it: ts, kind, target and the whole line as action_json."""
    line = {"ts": at, **action}
    return {"ts": at, "kind": action["kind"], "target": action.get("target", ""), "action_json": json.dumps(line)}


LAUNCH = _row("2026-11-09T01:00:00Z", kind="launch_epic", status="done", initiative="init-a", run="init-a-1")
LAND_P1 = _row("2026-11-09T02:30:00Z", kind="land_phase", status="landed", initiative="init-a", phase="p1", run="init-a-1")
LAND_P2 = _row("2026-11-09T03:00:00Z", kind="land_phase", status="landed", initiative="init-a", phase="p2", run="init-a-1")
FINISHED = [
    _t("t1", "p1", "done", size="S"),
    _t("t2", "p2", "done", needs=["t1"], size="M"),
    _t("t3", "p3", "ready", needs=["t2"], size="S"),
]


def test_durations_come_from_the_launch_and_land_rows_the_tick_reads():
    records = _inputs(FINISHED, actions=[LAUNCH, LAND_P1, LAND_P2])["records"]
    assert [(r["size"], r["mix"], span_seconds(r)) for r in records] == [("S", "plain", 5400.0), ("M", "plain", 1800.0)]
    assert estimate_duration(records, "M", "plain", 60.0).seconds == 1800.0


def test_a_phase_starts_when_its_parent_lands_and_a_first_phase_when_the_run_launched():
    by_size = {r["size"]: r for r in _inputs(FINISHED, actions=[LAUNCH, LAND_P1, LAND_P2])["records"]}
    assert by_size["S"]["build_start"] == "2026-11-09T01:00:00Z"
    assert by_size["M"]["build_start"] == "2026-11-09T02:30:00Z"
    assert by_size["M"]["approve_time"] == "2026-11-09T03:00:00Z"


def test_a_refused_land_a_missing_launch_or_an_unknown_phase_gives_no_record():
    refused = _row("2026-11-09T02:30:00Z", kind="land_phase", status="refused", initiative="init-a", phase="p1", run="init-a-1")
    shapes = {("init-a", "p1"): ("S", "plain")}
    assert duration_records([LAUNCH, refused], shapes, {}) == []
    assert duration_records([LAND_P1], shapes, {}) == []
    assert duration_records([LAUNCH, LAND_P1], {}, {}) == []


def test_gather_facts_reads_schedule_inputs_from_the_recorded_launch_and_land_rows(tmp_path, monkeypatch):
    for task, phase, state, needs, size in (("a", "p1", "done", "", "S"), ("b", "p2", "done", "[a]", "M"), ("c", "p3", "ready", "[b]", "S")):
        extra = f"needs: {needs}\n" if needs else ""
        _write(tmp_path / f"work/x/{phase}/{task}.md", f"---\nid: {task}\nstate: {state}\nsize: {size}\n{extra}---\n\nbody\n")
    _write(tmp_path / "work/x/initiative.md", "---\nid: x\nrepo: /r\n---\n\nbody\n")
    actions = _recorded(
        tmp_path,
        monkeypatch,
        [
            ("2026-11-06T10:00:00Z", {"kind": "launch_epic", "status": "done", "initiative": "x", "run": "x-1"}),
            ("2026-11-06T11:00:00Z", {"kind": "land_phase", "status": "landed", "initiative": "x", "phase": "p1", "run": "x-1"}),
            ("2026-11-06T11:40:00Z", {"kind": "land_phase", "status": "landed", "initiative": "x", "phase": "p2", "run": "x-1"}),
        ],
    )
    inputs = gather_facts(_deps(tmp_path, actions=actions), NOW)["schedule_inputs"]
    assert [(p["id"], p["status"], p["parents"]) for p in inputs["initiatives"][0]["phases"]] == [("p3", "ready", [])]  # p2 is done, so it is no parent
    assert [(r["size"], span_seconds(r)) for r in inputs["records"]] == [("S", 3600.0), ("M", 2400.0)]
    assert inputs["initiatives"][0]["repo"] == "/r"


def test_gather_facts_has_no_schedule_inputs_without_ticket_rows(tmp_path):
    _write(tmp_path / "work/x/p1/a.md", "---\nid: a\nstate: ready\n---\n\nbody\n")
    assert "schedule_inputs" in gather_facts(_deps(tmp_path), NOW)
    assert "schedule_inputs" not in gather_facts(replace(_deps(tmp_path), tickets=None), NOW)


def _recorded(tmp_path, monkeypatch, lines):
    """Write each (stamp, action) through the real recorder into a sqlite `chair_actions` table, then read them back as the tick does."""
    db = tmp_path / "store.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE chair_actions (ts TEXT, kind TEXT, target TEXT, action_json TEXT)")
    clock = [NOW.strftime("%Y-%m-%dT%H:%M:%SZ")]

    def store(argv: list[str]) -> tuple[int, str]:
        line = json.loads(argv[-1])
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO chair_actions VALUES (?, ?, ?, ?)", (line["ts"], line["kind"], line.get("target", ""), json.dumps(line))
            )
        return 0, ""

    def open_store(runs_dir):
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        return conn, "?"

    monkeypatch.setattr(run_store, "_open", open_store)
    record = recorder(tmp_path, lambda: 4, lambda: clock[0], store=store, holder="h")
    for stamp, action in lines:
        clock[0] = stamp
        record(action)
    return windowed_actions(lambda n: read_chair_actions(tmp_path, n), NOW)


def test_an_absent_target_is_null_and_a_stated_one_is_the_earliest_open():
    work = [
        {"kind": "initiative", "initiative": "init-a", "priority": 2},
        {"kind": "initiative", "initiative": "init-b", "release_target": "2026-12-01T00:00:00Z"},
        {"kind": "initiative", "initiative": "init-a", "extra": {"release_target": "2026-11-20T00:00:00Z"}},
    ]
    assert _inputs(TWO_WAVES)["target"] is None
    assert _inputs(TWO_WAVES, work=work[:1])["target"] is None
    assert _inputs(TWO_WAVES, work=work)["target"] == "2026-11-20T00:00:00Z"
    assert _inputs(TWO_WAVES, work=work[:1])["initiatives"][0]["priority"] == 2


def test_hosts_load_and_mode_come_from_the_dispatch_facts():
    inputs = _inputs(TWO_WAVES)
    assert [(h["name"], len(h["lanes"]), h["local"]) for h in inputs["hosts"]] == [
        ("chair", 2, True), ("host-a", 3, False), ("host-b", 1, False),
    ]
    assert inputs["load"] == {"chair": 1, "host-a": 0, "host-b": 2}
    assert inputs["mode"] == "building"
    orchestrating = schedule_inputs_from_rows(
        [], [], [], {**DISPATCH, "local_lanes": "decompose"}, local="chair", repos={}, land_at={}, live_phases=()
    )
    assert orchestrating["mode"] == "orchestrating"


def test_schedule_updated_is_an_action_kind():
    assert "schedule_updated" in get_args(ActionKind)
