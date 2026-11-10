import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "dash_feed_board_v1.json"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
STATUSES = ("ready", "building", "approved", "merged", "landed", "quarantined", "dropped", "blocked")
# A parent shows the first status in this list that any of its children has.
ROLLUP_ORDER = ("building", "quarantined", "blocked", "ready", "approved", "merged", "landed", "dropped")
LANE_NODES = ("build", "review", "adversary")
TURN_KEYS = (
    "ts", "host", "source", "kind", "agent_id", "model", "run", "initiative", "task",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd",
)
TURN_SOURCES = ("lane", "session", "subagent")
SAMPLE_TURN = {
    "ts": "2026-11-06T11:59:00Z", "host": "host-b", "source": "lane", "kind": "build",
    "agent_id": "board-feed-1-build-1", "model": "claude-sonnet-5-5", "run": "board-feed-1",
    "initiative": "board-feed", "task": "board-render",
    "input_tokens": 1800, "output_tokens": 420, "cache_read_tokens": 61000, "cache_write_tokens": 2300,
    "cost_usd": 0.0875,
}

EPIC = {"id": str, "title": str, "status": str, "cost_usd": float, "counts": dict, "initiatives": list, "settled": dict}
SETTLED = {"landed": int, "dropped": int, "cost_usd": float}
INITIATIVE = {"id": str, "short_id": str, "status": str, "cost_usd": float, "counts": dict, "phases": list}
PHASE = {"id": str, "status": str, "cost_usd": float, "counts": dict, "tasks": list}
TASK = {"id": str, "status": str, "run": str, "host": str, "cost_usd": float}
MACHINE = {
    "name": str, "state": str, "lanes_in_use": int, "capacity": int, "login_ok": bool, "login_checked_at": str,
    "beat_age_s": int, "checkouts": dict, "lanes": list, "load": dict,
}
LANE = {"run": str, "initiative": str, "phase": str, "tasks": list, "elapsed_s": int, "cost_usd": float}
LOAD = {"cpu_pct": float, "load_avg_per_core": float, "mem_used_gb": float, "mem_total_gb": float}
POINT = {"at": str, "input": int, "output": int, "cache_read": int}
METER = {"used_fraction": float, "projected_fraction": float, "ceiling_fraction": float, "resets_at": str, "over_ceiling": bool}
AGENT = {
    "id": str, "kind": str, "model": str, "host": str, "serves": str,
    "tokens_per_min": int, "cost_usd": float, "elapsed_s": int, "context_fill": float,
}

DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))
EPICS = DATA["board"]["epics"]
INITIATIVES = [i for e in EPICS for i in e["initiatives"]]
PHASES = [p for i in INITIATIVES for p in i["phases"]]
TASKS = [t for p in PHASES for t in p["tasks"]]
MACHINE_ROW = DATA["machines"][0]
LIVE = DATA["live"]


def _types(obj):
    return {k: type(v) for k, v in obj.items()}


def _leaves(value):
    if isinstance(value, dict):
        return [leaf for v in value.values() for leaf in _leaves(v)]
    if isinstance(value, list):
        return [leaf for v in value for leaf in _leaves(v)]
    return [value]


def _at(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _rollup(statuses):
    present = set(statuses)
    return next(s for s in ROLLUP_ORDER if s in present)


def _task_counts(tasks):
    return {s: sum(t["status"] == s for t in tasks) for s in STATUSES}


def _tasks_under(node):
    if "tasks" in node:
        return node["tasks"]
    children = node["phases"] if "phases" in node else node["initiatives"]
    return [t for child in children for t in _tasks_under(child)]


def _children(node):
    return node.get("initiatives") or node.get("phases") or node["tasks"]


def _spaced(points, step):
    stamps = [_at(p["at"]) for p in points]
    return stamps[-1] == NOW and {b - a for a, b in pairwise(stamps)} == {step}


def test_the_fixture_has_the_three_top_level_keys():
    assert list(DATA) == ["board", "machines", "live"]
    assert list(DATA["board"]) == ["epics"]


def test_the_feed_has_no_nulls():
    assert not [leaf for leaf in _leaves(DATA) if leaf is None]


def test_board_nodes_carry_the_documented_keys_and_types():
    assert all(_types(e) == EPIC for e in EPICS)
    assert all(_types(i) == INITIATIVE for i in INITIATIVES)
    assert all(_types(p) == PHASE for p in PHASES)
    assert all(_types(t) == TASK for t in TASKS)
    assert all(_types(e["settled"]) == SETTLED for e in EPICS)
    assert all(e["settled"] == {"landed": 0, "dropped": 0, "cost_usd": 0.0} for e in EPICS)
    nodes = [*EPICS, *INITIATIVES, *PHASES]
    assert all(list(n["counts"]) == list(STATUSES) for n in nodes)
    assert all(type(v) is int for n in nodes for v in n["counts"].values())


def test_task_status_run_and_host():
    assert {t["status"] for t in TASKS} == set(STATUSES)
    assert all((t["run"] == "") == (t["host"] == "") for t in TASKS)
    assert [t["id"] for t in TASKS if t["run"]] == ["board-render"]


def test_initiatives_without_an_epic_sit_under_the_empty_id_epic():
    no_epic = [e for e in EPICS if e["id"] == ""]
    assert [(e["title"], len(e["initiatives"])) for e in no_epic] == [("no epic", 1)]
    assert [(e["id"], len(e["initiatives"])) for e in EPICS if e["id"]] == [("dash-board", 1)]


def test_a_parent_cost_and_counts_are_the_sum_of_its_children_counted_at_task_level():
    for node in [*PHASES, *INITIATIVES, *EPICS]:
        assert node["cost_usd"] == sum(c["cost_usd"] for c in _children(node))
        assert node["counts"] == _task_counts(_tasks_under(node))
    assert [(i["id"], i["cost_usd"]) for i in INITIATIVES] == [("board-feed", 1.75), ("cost-ledger", 2.75)]
    assert [e["cost_usd"] for e in EPICS] == [1.75, 2.75]


def test_a_parent_status_is_the_rollup_of_its_children():
    assert _rollup(["landed", "dropped", "merged"]) == "merged"
    for node in [*PHASES, *INITIATIVES, *EPICS]:
        assert node["status"] == _rollup(c["status"] for c in _children(node))


def test_the_machine_carries_the_existing_keys_lanes_and_load():
    assert len(DATA["machines"]) == 1
    assert _types(MACHINE_ROW) == MACHINE
    assert MACHINE_ROW["lanes_in_use"] == len(MACHINE_ROW["lanes"])
    assert all(_types(lane) == LANE for lane in MACHINE_ROW["lanes"])
    assert _types(MACHINE_ROW["load"]) == LOAD


def test_a_lane_names_tasks_the_board_shows_building_on_that_run_and_machine():
    by_id = {t["id"]: t for t in TASKS}
    for lane in MACHINE_ROW["lanes"]:
        assert lane["initiative"] in {i["id"] for i in INITIATIVES}
        assert lane["phase"] in {p["id"] for p in PHASES}
        assert [(by_id[t]["run"], by_id[t]["host"]) for t in lane["tasks"]] == [(lane["run"], MACHINE_ROW["name"])] * len(lane["tasks"])


def test_throughput_is_sixty_minute_points_ending_at_the_current_minute():
    throughput = LIVE["throughput"]
    assert list(throughput) == ["system", "hosts"]
    series = [throughput["system"], *(h["series"] for h in throughput["hosts"])]
    assert all(len(s) == 60 and _spaced(s, timedelta(minutes=1)) for s in series)
    assert all(_types(p) == POINT for s in series for p in s)
    assert all(list(_types(h)) == ["host", "series"] and type(h["host"]) is str for h in throughput["hosts"])


def test_burn_has_two_rates_and_two_meters():
    burn = LIVE["burn"]
    assert _types(burn) == {"usd_per_min_5m": float, "usd_per_min_15m": float, "meters": dict}
    assert list(burn["meters"]) == ["five_hour", "weekly"]
    assert all(_types(m) == METER for m in burn["meters"].values())


def test_agents_are_sorted_by_tokens_per_min_descending():
    agents = LIVE["agents"]
    assert all(_types(a) == AGENT for a in agents)
    assert [a["tokens_per_min"] for a in agents] == sorted((a["tokens_per_min"] for a in agents), reverse=True)
    assert {"chair", "operator", "subagent"} <= {a["kind"] for a in agents}
    assert set(LANE_NODES) & {a["kind"] for a in agents}
    assert all(0 <= a["context_fill"] <= 1 for a in agents)


def test_serves_is_the_task_id_else_the_initiative_id_else_empty():
    served = {a["serves"] for a in LIVE["agents"]}
    assert served <= {*(t["id"] for t in TASKS), *(i["id"] for i in INITIATIVES), ""}
    assert {"board-render", "board-feed", ""} == served


def test_per_minute_and_per_hour_series_have_sixty_and_twenty_four_points():
    per_minute = [LIVE["calls_per_min"], LIVE["cache_hit_pct"]]
    per_hour = [LIVE["lands_per_hour"], LIVE["quarantines_per_hour"]]
    assert all(len(s) == 60 and _spaced(s, timedelta(minutes=1)) for s in per_minute)
    assert all(len(s) == 24 and _spaced(s, timedelta(hours=1)) for s in per_hour)
    assert all(list(p) == ["at", "value"] and type(p["at"]) is str for s in [*per_minute, *per_hour] for p in s)
    assert all(type(p["value"]) in (int, float) for s in [*per_minute, *per_hour] for p in s)


def test_the_live_block_has_exactly_the_documented_keys():
    assert list(LIVE) == [
        "throughput", "burn", "agents", "calls_per_min", "cache_hit_pct", "lands_per_hour", "quarantines_per_hour",
    ]


def test_the_sample_turn_has_exactly_the_turn_keys_with_their_types():
    assert len(TURN_KEYS) == 14 == len(set(TURN_KEYS))
    assert set(SAMPLE_TURN) == set(TURN_KEYS)
    assert all(type(SAMPLE_TURN[k]) is str for k in TURN_KEYS[:9])
    assert all(type(SAMPLE_TURN[k]) is int for k in TURN_KEYS[9:13])
    assert type(SAMPLE_TURN["cost_usd"]) is float
    assert SAMPLE_TURN["source"] in TURN_SOURCES
    assert _at(SAMPLE_TURN["ts"]) < NOW
