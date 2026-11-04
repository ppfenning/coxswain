import pytest

from agent_tools import route


def test_both_keys_present_are_returned_on_the_flat_result():
    parsed = route.parse_profile("spend:\n  monthly_budget_usd: 200\n  budget_timezone: America/New_York\n")
    assert parsed["monthly_budget_usd"] == 200.0
    assert parsed["budget_timezone"] == "America/New_York"


def test_both_keys_absent_are_not_in_the_result():
    parsed = route.parse_profile("spend:\n  window_ceiling_usd: 40\n")
    assert "monthly_budget_usd" not in parsed
    assert "budget_timezone" not in parsed


@pytest.mark.parametrize("amount", ["0", "-5"])
def test_a_monthly_budget_of_zero_or_below_names_the_line(amount):
    with pytest.raises(route.ProfileError, match="line 3"):
        route.parse_profile(f"spend:\n  window_ceiling_usd: 40\n  monthly_budget_usd: {amount}\n")


def test_an_unknown_zone_names_the_line():
    with pytest.raises(route.ProfileError, match="line 3"):
        route.parse_profile("spend:\n  monthly_budget_usd: 200\n  budget_timezone: Mars/Olympus\n")
