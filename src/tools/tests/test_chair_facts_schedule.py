from agent_tools.chair_facts import duration_records, phase_row, release_target, schedule_hosts, schedule_inputs


def _t(id, phase, state="todo", needs=(), surfaces=(), initiative="init", **extra):
    return {"id": id, "initiative": initiative, "phase": phase, "state": state, "needs": list(needs), "surfaces": list(surfaces), **extra}


DISPATCH = {
    "max_in_flight": 2,
    "live_runs": 1,
    "hosts": [{"name": "host-a", "live_runs": 2, "capacity": 3, "capabilities": []}],
}


def _inputs(items, live=(), records=(), ledger=(), work=(), run_host=None):
    return schedule_inputs(items, {"init": "repo"}, {"init": 5}, live, records, ledger, DISPATCH, work, run_host or {})


def _phases(inputs, initiative="init"):
    return {p["id"]: p for i in inputs["initiatives"] if i["id"] == initiative for p in i["phases"]}


def test_two_waves_give_parents_and_statuses():
    items = [_t("a1", "p1", "ready"), _t("b1", "p2", "todo", needs=["a1"]), _t("c1", "p3", "todo", needs=["b1"])]
    phases = _phases(_inputs(items))
    assert {k: v["parents"] for k, v in phases.items()} == {"p1": [], "p2": ["p1"], "p3": ["p2"]}
    assert phases["p1"]["status"] == "ready"


def test_later_phase_with_only_todo_tickets_is_not_running():
    items = [_t("a1", "p1", "ready"), _t("b1", "p2", "todo", needs=["a1"])]
    assert _phases(_inputs(items))["p2"]["status"] == "waiting"
    assert phase_row([_t("x", "p2", "todo")], [], live=False) == "ready"
    assert phase_row([_t("x", "p2", "todo"), _t("y", "p2", "ready")], ["p1"], live=False) != "running"


def test_running_needs_a_live_run_or_a_building_task():
    items = [_t("a1", "p1", "ready"), _t("b1", "p2", "in_progress"), _t("c1", "p3", "todo", needs=["b1"])]
    inputs = _inputs(items, live=[("init", "p1")])
    assert {k: v["status"] for k, v in _phases(inputs).items()} == {"p1": "running", "p2": "running", "p3": "waiting"}
    assert [(r["initiative"], r["phase"]) for r in inputs["running"]] == [("init", "p1"), ("init", "p2")]


def test_blank_phase_live_pair_covers_no_phase():
    items = [_t("a1", "p1", "ready"), _t("b1", "p2", "todo", needs=["a1"])]
    phases = _phases(_inputs(items, live=[("init", "")]))
    assert {k: v["status"] for k, v in phases.items()} == {"p1": "ready", "p2": "waiting"}


def test_finished_phase_under_a_live_lane_is_complete_and_frees_its_child():
    items = [_t("a1", "p1", "done"), _t("b1", "p2", "todo", needs=["a1"])]
    phases = _phases(_inputs(items, live=[("init", "p1")]))
    assert set(phases) == {"p2"}
    assert phases["p2"]["status"] == "ready"
    assert phases["p2"]["parents"] == []


def test_running_phases_are_pinned_to_lanes_of_their_host():
    items = [_t("a1", "p1", "in_progress"), _t("b1", "p2", "in_progress"), _t("c1", "p3", "in_progress", initiative="other")]
    inputs = _inputs(items, run_host={"init": "", "other": "host-a"})
    assert [(r["initiative"], r["phase"], r["host"], r["lane"], r["finish"]) for r in inputs["running"]] == [
        ("init", "p1", "local", "local-1", None),
        ("init", "p2", "local", "local-2", None),
        ("other", "p3", "host-a", "host-a-1", None),
    ]
    lanes = {h["name"]: h["lanes"] for h in inputs["hosts"]}
    assert all(r["lane"] in lanes[r["host"]] for r in inputs["running"])


def test_running_phase_without_a_free_lane_is_not_pinned():
    items = [_t(f"t{n}", f"p{n}", "in_progress") for n in range(3)]
    assert [r["phase"] for r in _inputs(items)["running"]] == ["p0", "p1"]


def test_blocked_complete_and_done_parent():
    items = [_t("a1", "p1", "done"), _t("b1", "p2", "quarantined", needs=["a1"]), _t("c1", "p3", "todo", needs=["a1"])]
    phases = _phases(_inputs(items))
    assert set(phases) == {"p2", "p3"}
    assert phases["p2"]["status"] == "blocked"
    assert phases["p3"] == {"id": "p3", "status": "ready", "parents": [], "size": "", "mix": "", "surfaces": []}


def test_shared_surface_and_size_survive():
    items = [
        _t("a1", "p1", "ready", surfaces=["src/x.py"], size="s", mix="build"),
        _t("a2", "p1", "ready", surfaces=["src/y.py", "src/x.py"]),
        _t("z1", "q1", "ready", surfaces=["src/x.py"], initiative="other"),
    ]
    inputs = _inputs(items)
    assert _phases(inputs)["p1"]["surfaces"] == ["src/x.py", "src/y.py"]
    assert _phases(inputs)["p1"]["size"] == "s"
    assert _phases(inputs)["p1"]["mix"] == "build"
    assert _phases(inputs, "other")["q1"]["surfaces"] == ["src/x.py"]


def test_finished_phases_become_duration_records():
    records = [
        {"initiative": "i", "phase": "p1", "build_start": "2026-11-05T10:00:00+00:00", "approve_time": "2026-11-05T11:00:00+00:00"},
        {"initiative": "i", "phase": "p2", "build_start": "2026-11-05T10:00:00+00:00", "approve_time": "2026-11-05T11:00:00+00:00"},
    ]
    ledger = [{"initiative": "i", "phase": "p1", "size": "s", "mix": "build"}]
    assert duration_records(records, ledger) == [
        {"size": "s", "mix": "build", "build_start": "2026-11-05T10:00:00+00:00", "approve_time": "2026-11-05T11:00:00+00:00"}
    ]
    assert _inputs([_t("a", "p1", "ready")], records=records, ledger=ledger)["records"] == duration_records(records, ledger)


def test_absent_target_is_none_and_stated_target_is_read():
    assert _inputs([_t("a", "p1", "ready")])["target"] is None
    assert release_target([{"kind": "initiative", "extra": {}}, {"kind": "task", "extra": {"target_release": "9"}}]) is None
    version = {"kind": "initiative", "extra": {"target_release": "2026.11.6"}}
    assert release_target([version]) is None
    dates = [
        {"kind": "initiative", "extra": {"target_release": "2026-11-10"}},
        {"kind": "initiative", "extra": {"target_release": "2026-11-09T12:00:00+02:00"}},
        version,
    ]
    assert release_target(dates) == "2026-11-09T10:00:00Z"
    assert _inputs([_t("a", "p1", "ready")], work=dates)["target"] == "2026-11-09T10:00:00Z"


def test_hosts_load_and_mode():
    hosts, load, mode = schedule_hosts(DISPATCH)
    assert [(h["name"], len(h["lanes"]), h["local"]) for h in hosts] == [("local", 2, True), ("host-a", 3, False)]
    assert load == {"local": 1, "host-a": 2}
    assert mode == "building"
    assert schedule_hosts({**DISPATCH, "local_lanes": "decompose"})[2] == "orchestrating"
