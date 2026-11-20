from tests.startup_timing import (
    CHAIR_STATUS_MIN_EXCESS_S,
    CHAIR_STATUS_MULTIPLE,
    CHAIR_STATUS_TARGET_S,
    HELP_TARGET_S,
    check_rounds,
)


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


def _chair_status(rounds):
    return check_rounds(rounds, CHAIR_STATUS_MULTIPLE, CHAIR_STATUS_TARGET_S, CHAIR_STATUS_MIN_EXCESS_S).ok


def test_the_ci_flakes_at_the_restored_cost_pass_the_chair_status_bound():
    # run 44405174922 (8.17x) and run 44404132512 (8.09x), as medians.
    assert _chair_status([(0.139, 0.017)] * 7) is True
    assert _chair_status([(0.198, 0.025)] * 7) is True


def test_a_tiny_baseline_that_lifts_the_ratio_past_the_multiple_alone_passes():
    # 11.3x, but cox is only 123 ms over the baseline: a fast runner's jitter, not a slower cox.
    assert _chair_status([(0.135, 0.012)] * 7) is True


def test_a_2x_chair_status_regression_fails_on_a_quiet_host_and_on_a_loaded_one():
    # Medians measured with cox run twice per sample: quiet 214 ms over 17 ms, every core busy 464 ms over 33 ms.
    assert _chair_status([(0.214, 0.0168)] * 7) is False
    assert _chair_status([(0.464, 0.0329)] * 7) is False


def test_the_failure_message_names_the_floor_when_one_is_set():
    message = check_rounds([(0.30, 0.02)] * 3, 10.5, CHAIR_STATUS_TARGET_S, 0.150).message
    assert message == (
        "cox took a median 0.300s against a 0.020s interpreter baseline over 3 interleaved rounds: "
        "median ratio 15.00x exceeds the 10.50x multiple and its median excess 0.280s exceeds the 0.150s floor "
        "(documented local target 0.30s)"
    )
