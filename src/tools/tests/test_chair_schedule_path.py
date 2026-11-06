def test_a_diamond_takes_the_heavier_branch():
    from agent_tools.chair_schedule_path import critical_paths

    weights = {"a": 1, "b": 5, "c": 2, "d": 3}
    dependents = {"a": ["b", "c"], "b": ["d"], "c": ["d"]}
    result = critical_paths(weights, dependents)
    assert result.values == {"a": 9, "b": 8, "c": 5, "d": 3}
    assert result.chain == ("a", "b", "d")
    assert result.cycle == ()


def test_a_chain_beats_a_wide_fan():
    from agent_tools.chair_schedule_path import critical_paths

    weights = {"x": 4, "y": 4, "z": 4, "h": 1, "f1": 1, "f2": 1, "f3": 1, "f4": 1, "f5": 1}
    dependents = {"x": ["y"], "y": ["z"], "h": ["f1", "f2", "f3", "f4", "f5"]}
    result = critical_paths(weights, dependents)
    assert result.values["x"] == 12
    assert result.values["h"] == 2
    assert result.chain == ("x", "y", "z")


def test_a_lone_node_is_its_own_weight():
    from agent_tools.chair_schedule_path import critical_paths

    result = critical_paths({"solo": 7}, {})
    assert result.values == {"solo": 7}
    assert result.chain == ("solo",)


def test_a_cycle_is_named_not_raised():
    from agent_tools.chair_schedule_path import critical_paths

    result = critical_paths({"a": 1, "b": 1, "c": 1, "d": 1}, {"d": ["c"], "c": ["a"], "a": ["b"], "b": ["c"]})
    assert result.cycle == ("a", "b", "c")
    assert result.values == {}
    assert result.chain == ()


def test_a_self_loop_is_a_cycle():
    from agent_tools.chair_schedule_path import critical_paths

    assert critical_paths({"a": 1}, {"a": ["a"]}).cycle == ("a",)


def test_a_zero_weight_root_stays_at_the_head_of_the_chain():
    from agent_tools.chair_schedule_path import critical_paths

    result = critical_paths({"z": 0, "b": 5}, {"z": ["b"]})
    assert result.values == {"z": 5, "b": 5}
    assert result.chain == ("z", "b")


def test_a_key_with_no_weight_is_counted_as_zero_and_reported():
    from agent_tools.chair_schedule_path import critical_paths

    result = critical_paths({"a": 2}, {"a": ["typo"]})
    assert result.values == {"a": 2, "typo": 0}
    assert result.unweighted == ("typo",)
    assert critical_paths({"a": 2}, {}).unweighted == ()


def test_ties_resolve_by_sorted_key_whatever_the_insertion_order():
    from agent_tools.chair_schedule_path import critical_paths

    forward = critical_paths({"m": 2, "k": 2, "c": 1, "b": 1}, {"m": ["c", "b"], "k": ["c", "b"]})
    backward = critical_paths({"b": 1, "c": 1, "k": 2, "m": 2}, {"k": ["b", "c"], "m": ["b", "c"]})
    assert forward.chain == ("k", "b")
    assert backward.chain == forward.chain
    assert backward.values == forward.values
