from tests.startup_timing import CHAIR_STATUS_TARGET_S, HELP_TARGET_S, check_rounds


def test_passes_under_the_multiple():
    assert check_rounds([(0.05, 0.02)] * 5, 4.0, HELP_TARGET_S).ok is True


def test_fails_over_the_multiple():
    assert check_rounds([(0.15, 0.02)] * 5, 4.0, HELP_TARGET_S).ok is False


def test_load_that_moves_between_rounds_does_not_move_the_verdict():
    # Every round is 5x. The cox column's median over the baseline column's median is 0.25 / 0.02 = 12.5x, which is
    # what timing the two as separate blocks could read on a host whose load rose between them.
    rounds = [(0.10, 0.02), (0.10, 0.02), (0.25, 0.05), (0.25, 0.05), (0.25, 0.05)]
    assert check_rounds(rounds, 8.0, HELP_TARGET_S).ok is True


def test_one_round_caught_by_a_burst_does_not_fail_the_check():
    rounds = [(0.10, 0.02)] * 4 + [(0.60, 0.02)]
    assert check_rounds(rounds, 8.0, HELP_TARGET_S).ok is True


def test_no_usable_round_fails_and_says_so():
    result = check_rounds([(0.1, 0.0)], 8.0, HELP_TARGET_S)
    assert result.ok is False
    assert result.message == "no round with a positive baseline among 1"


def test_failure_message_names_measurements_and_absolute_target():
    message = check_rounds([(0.15, 0.02)] * 3, 4.0, CHAIR_STATUS_TARGET_S).message
    assert message == (
        "cox took a median 0.150s against a 0.020s interpreter baseline over 3 interleaved rounds: "
        "median ratio 7.50x exceeds the 4.00x multiple (documented local target 0.30s)"
    )
