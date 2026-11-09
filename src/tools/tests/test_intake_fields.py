import pytest

from agent_tools.intake_fields import normalise, validate

REPOS = {"web", "api", "dash"}
EPICS = {"zeta", "alpha"}
FULLWIDTH_VERSION = "".join(chr(code) for code in (0xFF12, 0xFF10, 0xFF12, 0xFF16)) + ".11.6"
SHAPE = "dotted numeric version such as 2026.11.6 or v2026.11.6"


def test_all_valid_is_empty():
    assert validate("api", "alpha", 3, "v2026.11.6", REPOS, EPICS) == []


def test_optional_fields_omitted_is_empty():
    assert validate("api", None, None, None, REPOS, set()) == []


def test_unknown_repo_names_value_and_sorted_choices():
    assert validate("nope", None, None, None, REPOS, EPICS) == ["repo 'nope' is not known; valid repos: api, dash, web"]


def test_unknown_repo_with_no_repos_says_none_known():
    assert validate("nope", None, None, None, set(), EPICS) == ["repo 'nope' is not known; no repo is known yet"]


def test_unknown_epic_lists_sorted_epics():
    assert validate("api", "beta", None, None, REPOS, EPICS) == ["epic 'beta' is not known; valid epics: alpha, zeta"]


def test_unknown_epic_with_no_epics_says_none_exist():
    assert validate("api", "beta", None, None, REPOS, set()) == ["epic 'beta' is not known; no epic exists yet"]


@pytest.mark.parametrize("priority", [True, "high", 1.5])
def test_non_integer_priority_refused_with_ordering(priority):
    assert validate("api", None, priority, None, REPOS, EPICS) == [
        f"priority {priority!r} must be a whole number; higher runs first"
    ]


@pytest.mark.parametrize("priority", [0, "0", "3", -2])
def test_whole_number_priority_accepted(priority):
    assert validate("api", None, priority, None, REPOS, EPICS) == []


@pytest.mark.parametrize(
    "release", ["next", "2026.11.", "2026", "2026.11.6\n", FULLWIDTH_VERSION, "vv2026.11.6", 2026.11]
)
def test_malformed_version_refused_with_shape(release):
    assert validate("api", None, None, release, REPOS, EPICS) == [f"target_release {release!r} must be a {SHAPE}"]


@pytest.mark.parametrize("release", ["2026.11.6", "v2026.11.6", "1.0"])
def test_dotted_version_accepted(release):
    assert validate("api", None, None, release, REPOS, EPICS) == []


def test_every_bad_field_reports_its_own_line():
    assert len(validate("x", "y", "z", "w", REPOS, EPICS)) == 4


def test_normalise_casts_strips_and_drops_empty():
    assert normalise("api", "", "3", "v2026.11.6") == {
        "repo": "api",
        "priority": 3,
        "target_release": "2026.11.6",
    }


def test_normalise_keeps_priority_zero():
    assert normalise("api", "alpha", 0, None) == {"repo": "api", "epic": "alpha", "priority": 0}


def test_normalise_of_accepted_values_is_clean():
    assert validate("api", "alpha", "7", "v2026.11.6", REPOS, EPICS) == []
    assert normalise("api", "alpha", "7", "v2026.11.6") == {
        "repo": "api",
        "epic": "alpha",
        "priority": 7,
        "target_release": "2026.11.6",
    }
