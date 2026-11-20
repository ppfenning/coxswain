import pytest

from tests.startup_load_verdict import check_clean, ratios

CEILING = 10.0
STABLE = [(0.8, 0.1), (0.82, 0.1), (0.78, 0.1), (0.81, 0.1), (0.79, 0.1)]


def test_stable_ratio_near_8x_passes():
    verdict = check_clean(STABLE, CEILING)
    assert verdict.ok, str(verdict)
    assert verdict.median_ratio == pytest.approx(8.0)


def test_uniform_slow_host_gives_same_verdict():
    slow = [(cox * 1.5, ref * 1.5) for cox, ref in STABLE]
    verdict = check_clean(slow, CEILING)
    assert verdict.ok, str(verdict)
    assert verdict.median_ratio == pytest.approx(check_clean(STABLE, CEILING).median_ratio)


def test_one_outlier_round_does_not_flip_median():
    verdict = check_clean([*STABLE, (30.0, 0.1)], CEILING)
    assert verdict.ok, str(verdict)


def test_empty_input_fails_without_raising():
    assert ratios([]) == ((), None)
    assert not check_clean([], CEILING).ok
