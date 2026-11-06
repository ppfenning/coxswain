"""The merge queue settings: batch_size and wait_s from the profile, and the collect-then-open rule."""
import dataclasses

import pytest

from agent_tools.land_batch_settings import MergeQueue, merge_queue_setting, should_open


def test_defaults_when_key_absent_or_profile_none():
    assert merge_queue_setting(None) == MergeQueue(1, 0.0)
    assert merge_queue_setting({}) == MergeQueue(1, 0.0)
    assert merge_queue_setting({"other": 1}) == MergeQueue(1, 0.0)


def test_valid_values():
    assert merge_queue_setting({"merge_queue": {"batch_size": 3, "wait_s": 2.5}}) == MergeQueue(3, 2.5)
    assert merge_queue_setting({"merge_queue": {"batch_size": 2, "wait_s": 0}}) == MergeQueue(2, 0.0)


@pytest.mark.parametrize("bad", [0, -1, "2", True, 2.0, None])
def test_invalid_batch_size_falls_back_to_one(bad):
    assert merge_queue_setting({"merge_queue": {"batch_size": bad, "wait_s": 5}}) == MergeQueue(1, 5.0)


@pytest.mark.parametrize("bad", [-1, -0.5, "5", True, None, float("nan"), float("-inf")])
def test_invalid_wait_s_falls_back_to_zero(bad):
    assert merge_queue_setting({"merge_queue": {"batch_size": 4, "wait_s": bad}}) == MergeQueue(4, 0.0)


def test_both_keys_invalid_fall_back_independently_to_defaults():
    assert merge_queue_setting({"merge_queue": {"batch_size": True, "wait_s": "5"}}) == MergeQueue(1, 0.0)
    assert merge_queue_setting({"merge_queue": {"batch_size": 3}}) == MergeQueue(3, 0.0)
    assert merge_queue_setting({"merge_queue": {"wait_s": 7}}) == MergeQueue(1, 7.0)


def test_huge_int_wait_s_does_not_raise():
    settings = merge_queue_setting({"merge_queue": {"batch_size": 3, "wait_s": 10**400}})
    assert settings.wait_s == 10**400
    assert not should_open(2, 1e300, settings)


def test_infinite_wait_s_is_a_wait_that_never_ends():
    settings = merge_queue_setting({"merge_queue": {"batch_size": 3, "wait_s": float("inf")}})
    assert settings == MergeQueue(3, float("inf"))
    assert not should_open(2, 1e300, settings)
    assert should_open(3, 0.0, settings)


@pytest.mark.parametrize("bad", [None, 3, "x", ["batch_size"]])
def test_malformed_section_falls_back_to_defaults(bad):
    assert merge_queue_setting({"merge_queue": bad}) == MergeQueue(1, 0.0)


def test_non_mapping_profile_falls_back_to_defaults():
    assert merge_queue_setting(["merge_queue"]) == MergeQueue(1, 0.0)


def test_enabled_for_two_and_not_for_one():
    assert MergeQueue(2, 0.0).enabled
    assert not MergeQueue(1, 0.0).enabled


def test_merge_queue_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        MergeQueue(2, 0.0).batch_size = 3  # type: ignore[misc]


def test_should_open_when_batch_is_full():
    assert should_open(3, 0.0, MergeQueue(3, 10.0))
    assert should_open(4, 0.0, MergeQueue(3, 10.0))


def test_should_open_when_elapsed_equals_wait():
    assert should_open(1, 10.0, MergeQueue(3, 10.0))


def test_should_not_open_below_both():
    assert not should_open(2, 9.9, MergeQueue(3, 10.0))


def test_should_not_open_with_nothing_ready_even_after_wait():
    assert not should_open(0, 99.0, MergeQueue(3, 10.0))
    assert not should_open(0, 0.0, MergeQueue(1, 0.0))
