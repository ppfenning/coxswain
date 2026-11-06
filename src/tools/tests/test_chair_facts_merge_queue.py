import sys
from dataclasses import replace

import pytest

from agent_tools.chair_facts import gather_facts
from tests.test_chair_facts import NOW, _deps


def _with_queue(raw: object):
    return gather_facts(replace(_deps(), merge_queue=lambda: raw), NOW)


def test_a_valid_mapping_carries_batch_size_and_wait_s():
    assert _with_queue({"batch_size": 3, "wait_s": 90})["merge_queue"] == {"batch_size": 3, "wait_s": 90.0}


@pytest.mark.parametrize(
    "raw",
    [
        {"batch_size": 1, "wait_s": 90},
        {"batch_size": True, "wait_s": 90},
        {"batch_size": 2.5, "wait_s": 90},
        {"batch_size": "3", "wait_s": 90},
        {"wait_s": 90},
        {},
        "on",
        None,
    ],
)
def test_an_off_or_malformed_queue_leaves_the_key_out(raw):
    assert "merge_queue" not in _with_queue(raw)


@pytest.mark.parametrize("wait_s", ["soon", -1, True, None])
def test_a_valid_batch_size_with_a_bad_wait_s_falls_back_to_zero_wait(wait_s):
    assert _with_queue({"batch_size": 3, "wait_s": wait_s})["merge_queue"] == {"batch_size": 3, "wait_s": 0.0}


def test_a_missing_wait_s_falls_back_to_zero_wait():
    assert _with_queue({"batch_size": 2})["merge_queue"] == {"batch_size": 2, "wait_s": 0.0}


@pytest.mark.parametrize("wait_s", [10**400, float("inf")])
def test_a_wait_s_past_the_float_range_is_capped_and_does_not_raise(wait_s):
    assert _with_queue({"batch_size": 3, "wait_s": wait_s})["merge_queue"] == {
        "batch_size": 3,
        "wait_s": sys.float_info.max,
    }


def test_an_absent_callable_leaves_the_facts_as_they_were():
    assert _deps().merge_queue is None
    assert "merge_queue" not in gather_facts(_deps(), NOW)
