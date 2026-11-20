"""The path reads the settings models share: dotted gets, the crew map, and a seat's field paths. No mocks."""

from agent_tools.editor_model import _MISSING, _crew, _get, _seat_field_paths


def test_get_walks_a_dotted_path_and_says_missing_rather_than_none():
    resolved = {"policy": {"review_tier": "2"}, "context": None}
    assert _get(resolved, "policy.review_tier") == "2"
    assert _get(resolved, "context") is None
    assert _get(resolved, "policy.absent") is _MISSING
    assert _get(resolved, "policy.review_tier.deeper") is _MISSING


def test_a_seat_under_the_mirrored_cast_key_still_reads_as_crew():
    resolved = {"cast": {"builder": {"enabled": True, "skills": ["writer"]}}}
    assert _get(resolved, "crew.builder.enabled") is True
    assert _get(resolved, "crew.builder.skills") == ["writer"]
    assert _crew(resolved) == {"builder": {"enabled": True, "skills": ["writer"]}}


def test_crew_prefers_crew_and_reads_an_empty_or_odd_value_as_no_seats():
    assert _crew({"crew": {"a": {}}, "cast": {"b": {}}}) == {"a": {}}
    assert _crew({"crew": None}) == {}
    assert _crew([]) == {}


def test_seat_field_paths_name_only_the_fields_a_seat_sets():
    resolved = {"crew": {"builder": {"enabled": False}, "reviewer": "off"}}
    assert _seat_field_paths(resolved, "builder", resolved["crew"]["builder"]) == ["crew.builder.enabled"]
    assert _seat_field_paths(resolved, "reviewer", "off") == ["crew.reviewer"]
