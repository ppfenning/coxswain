"""With `epic_stack` on, which branch an initiative forks from and bases its pull request on."""
from core.landing import stack_parent


def test_a_stacked_initiative_forks_from_the_first_unlanded_member_it_needs_else_from_the_epic_branch() -> None:
    members = {"a": {"branch": "init/a", "landed": False}, "c": {"branch": "init/c", "landed": True}, "d": {"branch": None, "landed": False}}
    assert stack_parent(["a"], members, "epic-x") == "init/a"
    assert stack_parent(["c", "a"], members, "epic-x") == "init/a", "a landed member is on the epic branch already"
    assert stack_parent(["c"], members, "epic-x") == "epic-x"
    assert stack_parent(["d", "elsewhere"], members, "epic-x") == "epic-x", "no branch yet, or not of this epic"
    assert stack_parent([], members, "epic-x") == "epic-x"
    assert stack_parent([], {}, None) is None


def test_the_batch_size_defaults_to_one_at_a_time_and_refuses_anything_but_a_whole_number() -> None:
    import pytest

    from core.landing import resolve_land_batch

    assert resolve_land_batch({}) == 1 and resolve_land_batch({"landing_areas": {}}) == 1
    assert resolve_land_batch({"landing_areas": {"land_batch": 4}}) == 4
    with pytest.raises(ValueError, match="landing_areas.land_batch must be a whole number of 1 or more"):
        resolve_land_batch({"landing_areas": {"land_batch": 0}})
