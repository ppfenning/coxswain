import copy

from agent_tools.chair_reload import (
    cartridge_restart_keys,
    changed_keys,
    digest,
    failed_line,
    reload_line,
    restart_keys,
)


def test_digest_is_stable_across_str_and_bytes():
    assert digest("a: 1\n") == digest(b"a: 1\n")
    assert digest("a: 1\n") != digest("a: 2\n")


def test_flipped_steward_flag_is_reported_by_its_dotted_path():
    assert changed_keys({"chair": {"steward": False}}, {"chair": {"steward": True}}) == ["chair.steward"]


def test_equal_mappings_give_an_empty_list():
    assert changed_keys({"a": {"b": 1}, "c": None}, {"a": {"b": 1}, "c": None}) == []


def test_added_and_removed_keys_are_both_reported_even_when_none():
    assert changed_keys({"gone": None, "same": 1}, {"new": None, "same": 1}) == ["gone", "new"]


def test_an_empty_table_filling_reports_only_the_new_leaf():
    assert changed_keys({"chair": {}}, {"chair": {"steward": True}}) == ["chair.steward"]


def test_changed_keys_does_not_mutate_its_arguments():
    old, new = {"chair": {"steward": False}}, {"chair": {"steward": True, "stale_days": 3}}
    before = copy.deepcopy((old, new))
    changed_keys(old, new)
    assert (old, new) == before


def test_restart_keys_keeps_harness_dir_and_drops_steward():
    assert restart_keys(["chair.steward", "harness_dir"]) == ["harness_dir"]


def test_restart_keys_keeps_a_key_below_a_restart_setting():
    assert restart_keys(["notify.ntfy"]) == ["notify.ntfy"]


def test_restart_keys_keeps_lane_hosts_a_caller_adds_from_the_raw_text():
    assert restart_keys(["lane_hosts"]) == ["lane_hosts"]


def test_cartridge_restart_keys_keeps_the_dispatch_cap_and_drops_a_live_key():
    assert cartridge_restart_keys(["land_at", "policy.dispatch.max_in_flight"]) == ["policy.dispatch.max_in_flight"]


def test_reload_line_is_empty_for_no_change():
    assert reload_line([], [], []) == ""


def test_reload_line_marks_a_restart_key_only():
    line = reload_line(["chair.steward", "harness_dir"], [], ["harness_dir"])
    assert line == "chair reload: profile chair.steward, harness_dir (needs restart)"


def test_reload_line_tells_profile_and_cartridge_apart():
    assert reload_line(["ids"], ["land_at"], []) == "chair reload: profile ids; cartridge land_at"


def test_reload_line_marks_a_cartridge_restart_key():
    line = reload_line([], ["policy.dispatch.max_in_flight"], ["policy.dispatch.max_in_flight"])
    assert line == "chair reload: cartridge policy.dispatch.max_in_flight (needs restart)"


def test_reload_line_does_not_mark_a_cartridge_key_spelled_like_a_profile_restart_key():
    assert reload_line([], ["team"], ["team"]) == "chair reload: cartridge team"


def test_failed_line_names_source_and_reason_on_one_line():
    line = failed_line("profile", "bad yaml")
    assert line == "chair reload: could not re-read profile: bad yaml; keeping the last good values"
