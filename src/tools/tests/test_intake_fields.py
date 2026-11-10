import pytest

from agent_tools.intake_fields import normalise, validate

REPOS = ("web", "api", "tools")  # deliberately unsorted, so the sorted listing is proven
EPICS = ("e-2", "e-1")


def test_all_valid_returns_no_refusals() -> None:
    assert validate("api", "e-1", 3, "v2026.11.6", REPOS, EPICS) == []


def test_optional_fields_may_be_absent() -> None:
    assert validate("api", None, None, None, REPOS, EPICS) == []
    assert validate("api", "", "", "", REPOS, EPICS) == []


def test_unknown_repo_names_value_and_lists_sorted_choices() -> None:
    assert validate("nope", None, None, None, REPOS, EPICS) == [
        "repo 'nope' is not known; valid repos: api, tools, web"
    ]


def test_unknown_epic_lists_sorted_ids() -> None:
    assert validate("api", "e-9", None, None, REPOS, EPICS) == ["epic 'e-9' is not known; valid epics: e-1, e-2"]


def test_unknown_epic_with_no_epics_says_none_exist() -> None:
    assert validate("api", "e-9", None, None, REPOS, ()) == ["epic 'e-9' is not known; no epic exists yet"]


@pytest.mark.parametrize("bad", [True, False, "urgent", "3.5", 3.0, 2.5, "1e3"])
def test_non_whole_priority_is_refused_and_says_higher_runs_first(bad: object) -> None:
    (refusal,) = validate("api", None, bad, None, REPOS, EPICS)
    assert "whole number" in refusal
    assert "higher runs first" in refusal


@pytest.mark.parametrize("good", [0, 7, -2, "3", "+3", "-2", " 4 "])
def test_whole_priority_is_accepted(good: object) -> None:
    assert validate("api", None, good, None, REPOS, EPICS) == []


@pytest.mark.parametrize("bad", ["next", "2026.11.", "v1", "1", ".1", "1..2", "v", "2026.11.x"])
def test_malformed_version_is_refused_with_the_expected_shape(bad: str) -> None:
    (refusal,) = validate("api", None, None, bad, REPOS, EPICS)
    assert "such as 2026.11.6 or v2026.11.6" in refusal


@pytest.mark.parametrize("good", ["2026.11.6", "v2026.11.6", "1.2", " v0.47.0 "])
def test_dotted_version_is_accepted(good: str) -> None:
    assert validate("api", None, None, good, REPOS, EPICS) == []


def test_every_bad_field_is_reported_together() -> None:
    assert len(validate("nope", "e-9", "x", "next", REPOS, EPICS)) == 4


def test_whitespace_is_ignored_the_same_way_by_validate_and_normalise() -> None:
    assert validate(" api ", " e-1 ", " 3 ", " v2026.11.6 ", REPOS, EPICS) == []
    assert normalise(" api ", " e-1 ", " 3 ", " v2026.11.6 ") == {
        "repo": "api",
        "epic": "e-1",
        "priority": 3,
        "target_release": "2026.11.6",
    }


def test_normalise_types_strips_v_and_drops_empties() -> None:
    assert normalise("api", "e-1", "3", "v2026.11.6") == {
        "repo": "api",
        "epic": "e-1",
        "priority": 3,
        "target_release": "2026.11.6",
    }
    assert normalise("api", "", None, "") == {"repo": "api"}
    assert normalise("api", None, 0, None) == {"repo": "api", "priority": 0}
