import datetime
import json

from agent_tools.chair_facts import dispatch_facts
from agent_tools.chair_plan_fill import host_free_slots
from agent_tools.launch_place import Placement, Refusal, format_refusal, place

NOW = datetime.datetime(2026, 10, 30, 12, 0, tzinfo=datetime.UTC)
FRESH = "2026-10-30T11:59:00+00:00"


def _row(name, cpu=None, capacity=2):
    versions = {"workspace_dir": "/srv/ws"} | ({} if cpu is None else {"load": {"cpu": cpu, "mem": 0.1}})
    return {"name": name, "ssh": name, "capacity": capacity, "weight": 1, "state": "active", "beat_at": FRESH, "versions_json": json.dumps(versions)}


def test_overloaded_host_gets_no_slot_and_the_next_is_chosen():
    rows = [_row("a", cpu=1.2, capacity=9), _row("b", cpu=0.3)]
    assert place(rows, {}, NOW, load_ceiling=0.8) == Placement("b", 0, 2)


def test_every_host_loaded_refuses_with_the_load_named():
    rows = [_row("a", cpu=1.2), _row("b", cpu=0.9)]
    refusal = place(rows, {}, NOW, load_ceiling=0.8)
    assert isinstance(refusal, Refusal)
    assert format_refusal(refusal) == ["a 0/2  (load cpu 1.20 over 0.80)", "b 0/2  (load cpu 0.90 over 0.80)"]


def test_no_ceiling_keeps_todays_choice():
    rows = [_row("a", cpu=1.2, capacity=9), _row("b", cpu=0.3)]
    assert place(rows, {}, NOW) == place(rows, {}, NOW, load_ceiling=None) == Placement("a", 0, 9)


def test_host_without_a_load_record_stays_eligible():
    rows = [_row("a", cpu=1.2), _row("b")]
    assert place(rows, {}, NOW, load_ceiling=0.8) == Placement("b", 0, 2)


def test_host_free_slots_zeroes_a_loaded_host_only_when_the_fact_is_set():
    rows = [_row("a", cpu=1.2), _row("b", cpu=0.3)]
    dispatch = dispatch_facts({"max_in_flight": 1}, ["a", "b"], {}, {"a": 2, "b": 2}, {"a": 1, "b": 1})
    free = lambda facts: {h[0]: h[3] for h in host_free_slots(facts)}  # noqa: E731
    assert free({"dispatch": dispatch, "login_hosts": rows, "host_load_ceiling": 0.8}) == {"a": 0, "b": 2}
    assert free({"dispatch": dispatch, "login_hosts": rows}) == {"a": 2, "b": 2}
