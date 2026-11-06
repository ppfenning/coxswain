from harness.lane_workers import (
    LANE_CAPACITY_VAR,
    XDIST_VAR,
    check_env,
    lane_workers,
    lane_workers_from,
    workers_env,
)


def test_the_variable_names():
    assert LANE_CAPACITY_VAR == "COX_LANE_CAPACITY"
    assert XDIST_VAR == "PYTEST_XDIST_AUTO_NUM_WORKERS"


def test_cores_are_divided_among_the_lanes():
    assert lane_workers(12, 3) == 4


def test_a_lane_never_gets_fewer_than_one_worker():
    assert lane_workers(2, 8) == 1


def test_a_capacity_below_one_counts_as_one():
    assert lane_workers(6, 0) == 6
    assert lane_workers(6, -4) == 6


def test_cores_below_one_count_as_one():
    assert lane_workers(0, 2) == 1
    assert lane_workers(-3, 1) == 1


def test_a_stated_capacity_gives_the_worker_count():
    assert lane_workers_from({LANE_CAPACITY_VAR: "3"}, 12) == 4


def test_an_absent_capacity_is_none():
    assert lane_workers_from({}, 12) is None


def test_an_empty_capacity_is_none():
    assert lane_workers_from({LANE_CAPACITY_VAR: ""}, 12) is None


def test_a_non_numeric_capacity_is_none():
    assert lane_workers_from({LANE_CAPACITY_VAR: "abc"}, 12) is None


def test_a_capacity_below_one_is_none():
    assert lane_workers_from({LANE_CAPACITY_VAR: "0"}, 12) is None
    assert lane_workers_from({LANE_CAPACITY_VAR: "-2"}, 12) is None


def test_workers_env_sets_the_xdist_variable():
    assert workers_env({}, 4) == {XDIST_VAR: "4"}


def test_an_operator_set_xdist_variable_is_left_alone():
    assert workers_env({XDIST_VAR: "16"}, 4) == {}


def test_workers_env_does_not_mutate_its_input():
    environ = {"HOME": "/h"}
    workers_env(environ, 4)
    assert environ == {"HOME": "/h"}


def test_check_env_adds_the_worker_count_to_the_whole_environment():
    assert check_env({"HOME": "/h"}, 20, 5) == {"HOME": "/h", XDIST_VAR: "4"}


def test_check_env_without_a_usable_capacity_is_none():
    assert check_env({"HOME": "/h"}, 20, 0) is None
    assert check_env({"HOME": "/h"}, 20, -1) is None
    assert check_env({"HOME": "/h"}, 20, None) is None


def test_check_env_keeps_an_operator_set_value():
    assert check_env({XDIST_VAR: "16"}, 20, 5) == {XDIST_VAR: "16"}


def test_check_env_takes_a_capacity_read_from_the_lane_variable():
    environ = {LANE_CAPACITY_VAR: "5"}
    assert check_env(environ, 20, int(environ[LANE_CAPACITY_VAR]))[XDIST_VAR] == "4"


def test_check_env_does_not_mutate_its_input():
    environ = {"HOME": "/h"}
    check_env(environ, 20, 5)
    assert environ == {"HOME": "/h"}
