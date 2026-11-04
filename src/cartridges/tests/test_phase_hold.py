from core.phase_hold import phase_holds


def test_nothing_awaiting_does_not_hold():
    assert phase_holds(0, 3) is False


def test_awaiting_with_nothing_buildable_holds():
    assert phase_holds(2, 0) is True


def test_awaiting_with_one_buildable_does_not_hold():
    assert phase_holds(1, 1) is False


def test_both_zero_does_not_hold():
    assert phase_holds(0, 0) is False
