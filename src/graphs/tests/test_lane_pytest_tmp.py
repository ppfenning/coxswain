from pathlib import Path

from harness.lane_pytest_tmp import basetemp_env, lane_scratch_dir


def test_an_empty_environ_gets_basetemp_and_tmpdir():
    assert basetemp_env({}, Path("/s")) == {"PYTEST_ADDOPTS": "--basetemp=/s", "TMPDIR": "/s"}


def test_existing_addopts_are_kept_and_appended_to():
    assert basetemp_env({"PYTEST_ADDOPTS": "-q"}, Path("/s")) == {
        "PYTEST_ADDOPTS": "-q --basetemp=/s",
        "TMPDIR": "/s",
    }


def test_an_operator_basetemp_wins():
    assert basetemp_env({"PYTEST_ADDOPTS": "-q --basetemp=/op"}, Path("/s")) == {"TMPDIR": "/s"}


def test_the_scratch_dir_is_under_the_run_dir():
    assert lane_scratch_dir(Path("/r"), "p1", "t1") == Path("/r/pytest-tmp/p1--t1")
