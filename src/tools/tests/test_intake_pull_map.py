from agent_tools.intake_pull_map import PullMap, map_fields

DEFAULT = PullMap()


def test_ready_label_makes_stage_ready():
    fields, notes = map_fields(["bug", "intake:ready"], None, DEFAULT)
    assert fields == {"stage": "ready", "priority": None, "epic": "", "target_release": ""}
    assert notes == []


def test_no_ready_label_is_draft():
    assert map_fields(["bug"], None, DEFAULT)[0]["stage"] == "draft"


def test_priority_label_gives_integer():
    assert map_fields(["priority:2"], None, DEFAULT)[0]["priority"] == 2


def test_bad_priority_label_is_ignored_for_the_next_one():
    assert map_fields(["priority:high", "priority:3"], None, DEFAULT)[0]["priority"] == 3
    assert map_fields(["priority:high"], None, DEFAULT)[0]["priority"] is None


def test_first_priority_in_sorted_order_wins():
    assert map_fields(["priority:5", "priority:1"], None, DEFAULT)[0]["priority"] == 1


def test_epic_label_gives_text():
    assert map_fields(["epic:search", "epic:auth"], None, DEFAULT)[0]["epic"] == "auth"


def test_version_milestone_is_target_release():
    fields, notes = map_fields([], "1.4.0", DEFAULT)
    assert fields["target_release"] == "1.4.0"
    assert notes == []


def test_non_version_milestone_maps_to_nothing_and_is_noted():
    fields, notes = map_fields([], "Backlog", DEFAULT)
    assert fields["target_release"] == ""
    assert notes == ["milestone 'Backlog' is not a version; target_release left empty"]


def test_custom_mapping_changes_label_names():
    custom = PullMap(ready_label="go", priority_prefix="p-", epic_prefix="e/")
    fields, _ = map_fields(["go", "p-7", "e/db", "intake:ready"], None, custom)
    assert fields == {"stage": "ready", "priority": 7, "epic": "db", "target_release": ""}


def test_milestone_not_release_is_ignored_without_note():
    off = PullMap(milestone_is_release=False)
    assert map_fields([], "1.4.0", off) == (
        {"stage": "draft", "priority": None, "epic": "", "target_release": ""},
        [],
    )


def test_profile_builds_from_dict_and_ignores_absent_keys():
    assert PullMap.from_profile({"ready_label": "go"}) == PullMap(ready_label="go")
    assert PullMap.from_profile({}) == PullMap()


def test_profile_refuses_unknown_key():
    assert PullMap.from_profile({"bogus": "x"}) == "unknown pull-map key: bogus"


def test_profile_refuses_non_string_value():
    assert PullMap.from_profile({"epic_prefix": 3}) == "pull-map key epic_prefix must be a string"
