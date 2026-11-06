import json

from agent_tools.host_load import LOAD_KEY, load_report, overloaded, resolve_load_ceiling


def test_load_key():
    assert LOAD_KEY == "load"


def test_load_report_divides_load_by_cores_and_counts_memory_in_use():
    assert load_report(2.0, 4, 1000, 250) == {"cpu": 0.5, "mem": 0.75}


def test_load_report_rounds_to_two_decimals():
    assert load_report(1.0, 3, 3, 1) == {"cpu": 0.33, "mem": 0.67}


def test_load_report_zero_cores_gives_zero_cpu():
    assert load_report(2.0, 0, 1000, 250)["cpu"] == 0.0


def test_load_report_zero_memory_total_gives_zero_mem():
    assert load_report(2.0, 4, 0, 0)["mem"] == 0.0


def test_resolve_load_ceiling_accepts_a_positive_number_or_string():
    assert resolve_load_ceiling("1.5") == 1.5
    assert resolve_load_ceiling(2) == 2.0


def test_resolve_load_ceiling_none_for_zero_negative_junk_and_missing():
    assert [resolve_load_ceiling(v) for v in (0, -1, "abc", "", None, True, float("nan"))] == [None] * 7


def test_overloaded_names_cpu_over_ceiling():
    rows = [{"name": "a", "versions_json": {"load": {"cpu": 1.4, "mem": 0.2}}}]
    assert overloaded(rows, 1.0) == {"a": "load cpu 1.40 over 1.00"}


def test_overloaded_names_mem_over_ceiling():
    rows = [{"name": "b", "versions_json": {"load": {"cpu": 0.2, "mem": 0.95}}}]
    assert overloaded(rows, 0.9) == {"b": "load mem 0.95 over 0.90"}


def test_overloaded_names_cpu_when_both_are_over():
    rows = [{"name": "c", "versions_json": {"load": {"cpu": 2.0, "mem": 2.0}}}]
    assert overloaded(rows, 1.0) == {"c": "load cpu 2.00 over 1.00"}


def test_overloaded_does_not_list_a_host_at_the_ceiling():
    rows = [{"name": "d", "versions_json": {"load": {"cpu": 1.0, "mem": 1.0}}}]
    assert overloaded(rows, 1.0) == {}


def test_overloaded_reads_a_json_string():
    rows = [{"name": "e", "versions_json": json.dumps({"load": {"cpu": 1.4, "mem": 0.1}})}]
    assert overloaded(rows, 1.0) == {"e": "load cpu 1.40 over 1.00"}


def test_overloaded_skips_missing_and_malformed_records():
    rows = [
        {"name": "none", "versions_json": None},
        {"name": "empty", "versions_json": {}},
        {"name": "badjson", "versions_json": "{not json"},
        {"name": "notmap", "versions_json": '["x"]'},
        {"name": "short", "versions_json": {"load": {"cpu": 5.0}}},
        {"name": "text", "versions_json": {"load": {"cpu": "hot", "mem": 0.1}}},
        {"name": "flag", "versions_json": {"load": {"cpu": True, "mem": 0.1}}},
        {"name": "scalar", "versions_json": {"load": 7}},
    ]
    assert overloaded(rows, 1.0) == {}


def test_overloaded_with_no_ceiling_lists_nothing():
    rows = [{"name": "f", "versions_json": {"load": {"cpu": 9.0, "mem": 9.0}}}]
    assert overloaded(rows, None) == {}
