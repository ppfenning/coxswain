from agent_tools import land

PHASES = {"a": "p1", "b": "p2", "c": "p2", "d": "p10", "e": "p11"}


def test_unlanded_parent_need_names_the_parent_phase():
    assert land.parent_land_wait(["b"], PHASES, {"b": ["a"]}, set()) == "waiting on parent land: p1"


def test_landed_parent_need_is_met():
    assert land.parent_land_wait(["b"], PHASES, {"b": ["a"]}, {"a"}) is None


def test_same_phase_need_never_counts():
    assert land.parent_land_wait(["b"], PHASES, {"b": ["c"]}, set()) is None


def test_carried_need_is_met():
    assert land.parent_land_wait(["b"], PHASES, {"b": ["a"]}, set(), carried={"a"}) is None


def test_first_unmet_parent_in_natural_order_is_named():
    assert land.parent_land_wait(["e"], PHASES, {"e": ["d", "b"]}, set()) == "waiting on parent land: p2"


def test_need_missing_from_phase_map_is_ignored():
    assert land.parent_land_wait(["b"], PHASES, {"b": ["zz"]}, set()) is None
