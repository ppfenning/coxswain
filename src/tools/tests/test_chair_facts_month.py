from dataclasses import replace
from datetime import timedelta

from agent_tools import budget, pacing
from agent_tools.chair_facts import gather_facts
from tests.test_chair_facts import NOW, POLICY, _deps

UNLIMITED = pacing.Window(NOW - timedelta(hours=1), NOW + timedelta(hours=4), 0.0, None, 0.0, 0)


def _month_only(spent_usd: float):
    month = budget.month_budget(spent_usd, 100.0, NOW, "UTC", POLICY.min_elapsed_fraction)
    return replace(_deps(), window=lambda: UNLIMITED, weekly=lambda: None, month=lambda: month)


def test_month_at_the_fraction_hard_stops_and_caps_launches_at_zero():
    limits = gather_facts(_month_only(85.0), NOW)["limits"]
    assert limits["hard_stop"] is True
    assert limits["launch_cap"] == 0
    assert limits["month_fraction"] == 0.85
    assert limits["month_verdict"] == "over_pace"


def test_month_below_the_fraction_keeps_a_nonzero_cap():
    limits = gather_facts(_month_only(84.0), NOW)["limits"]
    assert limits["hard_stop"] is False
    assert limits["launch_cap"] > 0
    assert limits["month_fraction"] == 0.84


def test_no_budget_leaves_the_facts_as_they_were():
    assert gather_facts(replace(_deps(), month=lambda: None), NOW) == gather_facts(_deps(), NOW)
    assert "month_fraction" not in gather_facts(_deps(), NOW)["limits"]
    assert "month_verdict" not in gather_facts(_deps(), NOW)["limits"]
