import pytest

from agent_tools import route
from agent_tools.chair_facts import resolve_steward_enabled, steward_asked_for


def _parse(text: str) -> dict:
    return route._parse_profile(text)[0]


@pytest.mark.parametrize(
    ("word", "expected"),
    [("true", True), ("false", False), ("yes", True), ("no", False), ("True", True), ("FALSE", False), ("Yes", True), ("NO", False)],
)
def test_steward_parses_to_a_python_bool(word, expected):
    parsed = _parse(f"chair:\n  steward: {word}\n")
    assert parsed == {"chair": {"steward": expected}, "assume": "a"}
    assert type(parsed["chair"]["steward"]) is bool


def test_steward_beside_a_float_key_keeps_both_types():
    chair = _parse("chair:\n  stale_days: 14\n  steward: true\n")["chair"]
    assert chair == {"stale_days": 14.0, "steward": True}
    assert type(chair["stale_days"]) is float
    assert type(chair["steward"]) is bool


def test_an_invalid_steward_value_names_its_line():
    with pytest.raises(route.ProfileError, match=r"line 3:\s+steward: maybe"):
        _parse("chair:\n  stale_days: 14\n  steward: maybe\n")


def test_an_empty_steward_value_raises():
    with pytest.raises(route.ProfileError, match="line 2"):
        _parse("chair:\n  steward:\n")


def test_colon_bearing_comments_leave_the_result_unchanged():
    plain = "chair:\n  stale_days: 7\n  steward: true\n"
    commented = (
        "chair:\n"
        "  # note: steward on\n"
        "  stale_days: 7  # note: days\n"
        "    # note: indented deeper\n"
        "  steward: true  # note: steward off\n"
    )
    assert _parse(commented) == _parse(plain)


def test_the_parsed_steward_is_read_as_asked_for_and_turns_nothing_on():
    on = _parse("chair:\n  steward: true\n")["chair"]["steward"]
    off = _parse("chair:\n  steward: false\n")["chair"]["steward"]
    assert (steward_asked_for(on), steward_asked_for(off)) == (True, False)
    assert (resolve_steward_enabled(on), resolve_steward_enabled(off)) == (False, False)


@pytest.mark.parametrize("word", ["all", "epics"])
def test_steward_scope_parses_to_its_string(word):
    assert _parse(f"chair:\n  steward_scope: {word}\n")["chair"] == {"steward_scope": word}


@pytest.mark.parametrize(("written", "expected"), [("Epics", "epics"), ("ALL", "all"), ("EpIcS", "epics")])
def test_steward_scope_is_lower_cased(written, expected):
    assert _parse(f"chair:\n  steward_scope: {written}\n")["chair"]["steward_scope"] == expected


@pytest.mark.parametrize(("written", "expected"), [('"epics"', "epics"), ("'all'", "all")])
def test_steward_scope_strips_quotes(written, expected):
    assert _parse(f"chair:\n  steward_scope: {written}\n")["chair"]["steward_scope"] == expected


def test_steward_and_steward_scope_keep_their_types():
    chair = _parse("chair:\n  steward: true\n  steward_scope: epics\n")["chair"]
    assert chair == {"steward": True, "steward_scope": "epics"}
    assert type(chair["steward"]) is bool
    assert type(chair["steward_scope"]) is str


def test_an_unknown_steward_scope_names_its_line():
    with pytest.raises(route.ProfileError, match=r"line 3:\s+steward_scope: everything"):
        _parse("chair:\n  stale_days: 14\n  steward_scope: everything\n")


def test_an_empty_steward_scope_raises():
    with pytest.raises(route.ProfileError, match="line 2"):
        _parse("chair:\n  steward_scope:\n")


def test_steward_scope_is_absent_when_not_written():
    assert "steward_scope" not in _parse("chair:\n  stale_days: 7\n")["chair"]
