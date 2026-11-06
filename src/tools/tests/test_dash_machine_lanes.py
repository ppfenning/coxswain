import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from agent_tools.dash_machine_lanes import host_load, lane_entries

FIXTURE = Path(__file__).parent / "fixtures" / "dash_feed_board_v1.json"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
LOCAL = "host-a"


def lane(run, host, phase="p1", cost_usd=0.5):
    return SimpleNamespace(run=run, host=host, phase=phase, cost_usd=cost_usd, node="build", attempt=1, turns=3)


def entries(lanes, **overrides):
    args = {
        "host_of": lambda run: None,
        "initiative_of": {"r1": "init-x", "r2": "init-x", "r3": "init-y", "r4": "init-y"},
        "building_tasks": {},
        "started_at": {},
    }
    return lane_entries(lanes, **{**args, **overrides}, now=NOW, local_host=LOCAL)


def test_two_hosts_are_grouped_and_ordered_by_run():
    out = entries(
        [lane("r2", "host-b"), lane("r3", "host-a", "p9", 2.0), lane("r1", "host-b")],
        started_at={"r1": NOW - timedelta(seconds=840, milliseconds=900)},
    )
    assert out == {
        "host-a": [
            {"run": "r3", "initiative": "init-y", "phase": "p9", "tasks": [], "elapsed_s": 0, "cost_usd": 2.0},
        ],
        "host-b": [
            {"run": "r1", "initiative": "init-x", "phase": "p1", "tasks": [], "elapsed_s": 840, "cost_usd": 0.5},
            {"run": "r2", "initiative": "init-x", "phase": "p1", "tasks": [], "elapsed_s": 0, "cost_usd": 0.5},
        ],
    }


def test_lane_without_host_goes_to_host_of_then_local():
    out = entries(
        [lane("r1", None), lane("r2", ""), lane("r3", None)], host_of=lambda run: "host-c" if run == "r3" else None
    )
    assert {host: [e["run"] for e in es] for host, es in out.items()} == {"host-a": ["r1", "r2"], "host-c": ["r3"]}


def test_lane_with_two_building_tasks_and_a_mapping_lane():
    out = entries(
        [{"run": "r1", "host": "host-b", "phase": "p1", "cost_usd": 1}],
        building_tasks={("init-x", "p1"): ["t-a", "t-b"], ("init-x", "p2"): ["other"]},
    )
    assert out["host-b"][0]["tasks"] == ["t-a", "t-b"]
    assert out["host-b"][0]["cost_usd"] == 1.0


def test_missing_or_future_started_at_gives_zero_elapsed():
    out = entries(
        [lane("r1", "host-a"), lane("r2", "host-a"), lane("r3", "host-a")],
        started_at={"r1": None, "r3": NOW + timedelta(seconds=5)},
    )
    assert [e["elapsed_s"] for e in out["host-a"]] == [0, 0, 0]


def test_absent_values_are_blank_not_none():
    out = entries([{"run": "zz"}])
    assert out == {LOCAL: [{"run": "zz", "initiative": "", "phase": "", "tasks": [], "elapsed_s": 0, "cost_usd": 0.0}]}


def test_no_lanes_gives_empty_mapping():
    assert entries([]) == {}


def test_host_load_with_beat_figures():
    versions = {
        "host_facts": {"cpu_count": 8, "mem_total_gb": 32.0},
        "host_load": {
            "cpu_pct": 41.456,
            "load_avg_1m": 6.0,
            "load_avg_5m": 5.0,
            "load_avg_15m": 4.0,
            "mem_used_gb": 18.254,
        },
    }
    assert host_load(versions) == {
        "cpu_pct": 41.46,
        "load_avg_per_core": 0.75,
        "mem_used_gb": 18.25,
        "mem_total_gb": 32.0,
    }


def test_host_load_without_host_load_keeps_memory_total():
    versions = {"host_facts": {"cpu_count": 4, "mem_total_gb": 16}}
    assert host_load(versions) == {"cpu_pct": 0.0, "load_avg_per_core": 0.0, "mem_used_gb": 0.0, "mem_total_gb": 16.0}


def test_host_load_with_zero_cpu_count_has_zero_per_core():
    versions = {"host_facts": {"cpu_count": 0, "mem_total_gb": 8.0}, "host_load": {"cpu_pct": 10.0, "load_avg_1m": 3.0}}
    assert host_load(versions)["load_avg_per_core"] == 0.0


def test_host_load_with_missing_load_avg_has_zero_per_core():
    versions = {"host_facts": {"cpu_count": 4}, "host_load": {"cpu_pct": 10.0}}
    assert host_load(versions)["load_avg_per_core"] == 0.0


def test_empty_beat_gives_all_zeros():
    assert host_load({}) == {"cpu_pct": 0.0, "load_avg_per_core": 0.0, "mem_used_gb": 0.0, "mem_total_gb": 0.0}


def test_shapes_match_the_committed_fixture():
    machine = json.loads(FIXTURE.read_text())["machines"][0]
    entry = entries([lane("r1", "host-b")])["host-b"][0]
    assert set(entry) == set(machine["lanes"][0])
    assert set(host_load({})) == set(machine["load"])
    assert None not in [*entry.values(), *host_load({}).values()]
