from copy import deepcopy

from agent_tools.chair_steer_cycle import break_cycle, find_cycle


def _init(id: str, surfaces: list[str], repo: str = "r") -> dict:
    return {"id": id, "started": False, "ready_tasks": [], "landed": set(), "repo": repo, "ready_surfaces": surfaces}


def _held(id: str, surfaces: list[str], repo: str = "r", **extra) -> dict:
    return {"id": id, "repo": repo, "surfaces": surfaces, **extra}


def _deadlock(repo: str = "r") -> tuple[list[dict], list[dict]]:
    initiatives = [
        _init("A", ["a.py", "b.py"], repo),
        _init("B", ["b.py", "c.py"], repo),
        _init("C", ["c.py", "a.py"], repo),
    ]
    running = [_held(i, s, repo, live=False) for i, s in (("A", ["a.py"]), ("B", ["b.py"]), ("C", ["c.py"]))]
    return initiatives, running


def test_three_way_hold_is_a_cycle_and_empties_running():
    initiatives, running = _deadlock()
    facts = {"initiatives": initiatives, "running": running}
    assert find_cycle(facts) == {"repo": "r", "members": ["A", "B", "C"]}
    out, cycle = break_cycle(facts)
    assert cycle == {"repo": "r", "members": ["A", "B", "C"]}
    assert out["running"] == []


def test_break_does_not_mutate_its_input():
    initiatives, running = _deadlock()
    facts = {"initiatives": initiatives, "running": running}
    before = deepcopy(facts)
    out, _ = break_cycle(facts)
    assert facts == before
    assert out is not facts


def test_one_live_entry_means_no_cycle():
    initiatives, running = _deadlock()
    live = [{**running[0], "live": True}, *running[1:]]
    facts = {"initiatives": initiatives, "running": live}
    assert find_cycle(facts) is None
    assert break_cycle(facts) == (facts, None)
    assert break_cycle(facts)[0] is facts


def test_missing_live_flag_means_live():
    initiatives, running = _deadlock()
    unflagged = [{k: v for k, v in running[0].items() if k != "live"}, *running[1:]]
    assert find_cycle({"initiatives": initiatives, "running": unflagged}) is None


def test_only_the_deadlocked_repo_loses_entries():
    ia, ra = _deadlock("r1")
    ib, rb = _deadlock("r2")
    rb = [{**rb[0], "live": True}, *rb[1:]]
    facts = {"initiatives": ia + ib, "running": ra + rb}
    out, cycle = break_cycle(facts)
    assert cycle == {"repo": "r1", "members": ["A", "B", "C"]}
    assert out["running"] == rb


def test_initiative_with_no_overlap_means_no_cycle():
    initiatives, running = _deadlock()
    facts = {"initiatives": [*initiatives, _init("D", ["d.py"])], "running": running}
    assert find_cycle(facts) is None


def test_one_ready_initiative_is_not_a_cycle():
    facts = {"initiatives": [_init("A", ["a.py"])], "running": [_held("B", ["a.py"], live=False)]}
    assert find_cycle(facts) is None


def test_policy_limit_that_lets_each_through_means_no_cycle():
    initiatives, running = _deadlock()
    facts = {"initiatives": initiatives, "running": running, "steer_policy": {"limit": 2, "serial": []}}
    assert find_cycle(facts) is None


def test_serial_path_keeps_the_cycle_under_a_loose_limit():
    initiatives = [_init("A", ["s.py"]), _init("B", ["s.py"])]
    running = [_held("A", ["s.py"], live=False), _held("B", ["s.py"], live=False)]
    facts = {"initiatives": initiatives, "running": running, "steer_policy": {"limit": 5, "serial": ["s.py"]}}
    assert find_cycle(facts) == {"repo": "r", "members": ["A", "B"]}
