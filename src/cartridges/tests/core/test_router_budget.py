from core.router_budget import DEFAULT_FLOOR_USD, DEFAULT_MARGIN, budget_usd


def test_empty_history_returns_the_floor_and_names_the_missing_history():
    budget, reason = budget_usd((), 2.0, 0.5)
    assert budget == 0.5
    assert "no observed turn costs" in reason


def test_one_observation_is_its_own_median():
    assert budget_usd((1.0,), 2.0, 0.5)[0] == 2.0


def test_odd_count_takes_the_middle_value_of_unsorted_input():
    assert budget_usd((1.0, 5.0, 3.0), 1.0, 0.0)[0] == 3.0


def test_even_count_averages_the_two_middle_values():
    assert budget_usd((4.0, 1.0, 3.0, 2.0), 1.0, 0.0)[0] == 2.5


def test_margin_is_applied_to_the_median():
    budget, reason = budget_usd((2.0,), 1.5, 0.0)
    assert budget == 3.0
    assert "margin 1.5" in reason


def test_floor_lifts_a_budget_that_would_fall_below_it():
    budget, reason = budget_usd((0.1,), 2.0, 1.0)
    assert budget == 1.0
    assert "floor applied" in reason


def test_price_ratio_scales_the_budget_and_defaults_to_no_change():
    assert budget_usd((2.0,), 1.0, 0.0, price_ratio=0.5)[0] == 1.0
    assert budget_usd((2.0,), 1.0, 0.0)[0] == 2.0


def test_named_defaults_apply_when_margin_and_floor_are_omitted():
    assert budget_usd(())[0] == DEFAULT_FLOOR_USD
    assert budget_usd((1.0,))[0] == 1.0 * DEFAULT_MARGIN
