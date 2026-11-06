import json

from agent_tools.host_load import LOAD_KEY, load_report, overload_warnings, overloaded, resolve_load_ceiling


def test_load_key():
    assert LOAD_KEY == "load"


def test_load_report_divides_load_by_cores_and_counts_memory_in_use():
    assert load_report(2.0, 4, 1000, 250) == {"cpu": 0.5, "mem": 0.75, "load_1m": 2.0, "cores": 4}


def test_load_report_rounds_to_two_decimals():
    assert load_report(1.0, 3, 3, 1) == {"cpu": 0.33, "mem": 0.67, "load_1m": 1.0, "cores": 3}


def test_load_report_carries_raw_load_rounded_and_the_core_count():
    assert load_report(88.456, 20, 1000, 250)["load_1m"] == 88.46
    assert load_report(88.456, 20, 1000, 250)["cores"] == 20


def test_load_report_zeros_stay_zeros():
    assert load_report(0.0, 0, 0, 0) == {"cpu": 0.0, "mem": 0.0, "load_1m": 0.0, "cores": 0}


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


def _row(name, **load):
    return {"name": name, "versions_json": {"load": {"mem": 0.1, **load}}}


def test_overload_warnings_names_load_and_cores_for_88_on_20():
    rows = [_row("big", cpu=4.4, load_1m=88.0, cores=20)]
    assert overload_warnings(rows) == {"big": "load 88.0 on 20 cores is 4.40 per core, over 2.00x"}


def test_overload_warnings_skips_22_on_14():
    assert overload_warnings([_row("ok", cpu=1.57, load_1m=22.0, cores=14)]) == {}


def test_overload_warnings_does_not_warn_at_exactly_the_factor():
    assert overload_warnings([_row("edge", cpu=2.0, load_1m=40.0, cores=20)]) == {}


def test_overload_warnings_old_record_warns_on_cpu():
    assert overload_warnings([_row("old", cpu=2.5)]) == {"old": "load 2.50 per core, over 2.00x"}


def test_overload_warnings_zero_cores_falls_back_to_cpu():
    assert overload_warnings([_row("z", cpu=3.0, load_1m=3.0, cores=0)]) == {"z": "load 3.00 per core, over 2.00x"}


def test_overload_warnings_returns_only_the_overloaded_host():
    rows = [_row("big", cpu=4.4, load_1m=88.0, cores=20), _row("ok", cpu=1.57, load_1m=22.0, cores=14)]
    assert list(overload_warnings(rows)) == ["big"]


def test_overload_warnings_honours_a_custom_factor():
    assert overload_warnings([_row("a", cpu=1.5, load_1m=6.0, cores=4)], factor=1.0) == {
        "a": "load 6.0 on 4 cores is 1.50 per core, over 1.00x"
    }


def test_overload_warnings_reads_a_json_string():
    rows = [{"name": "j", "versions_json": json.dumps({"load": {"cpu": 3.0, "mem": 0.1}})}]
    assert overload_warnings(rows) == {"j": "load 3.00 per core, over 2.00x"}


def test_overload_warnings_skips_missing_and_malformed_records():
    rows = [
        {"name": "none", "versions_json": None},
        {"name": "badjson", "versions_json": "{not json"},
        {"name": "short", "versions_json": {"load": {"cpu": 9.0}}},
        {"name": "flag", "versions_json": {"load": {"cpu": True, "mem": 0.1}}},
    ]
    assert overload_warnings(rows) == {}
