import pytest

from agent_tools import route
from agent_tools.chair_facts import resolve_steward_enabled


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


def test_the_parsed_steward_reaches_resolve_steward_enabled():
    assert resolve_steward_enabled(_parse("chair:\n  steward: true\n")["chair"]["steward"]) is True
    assert resolve_steward_enabled(_parse("chair:\n  steward: false\n")["chair"]["steward"]) is False
