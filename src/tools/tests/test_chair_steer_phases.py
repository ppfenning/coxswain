from agent_tools.chair_steer import ESCALATE_AT, steer_check


def _run(name: str, surfaces: list[str], phase: str | None = None) -> dict:
    entry = {"id": name, "repo": "r", "surfaces": surfaces}
    return {**entry, "phase": phase} if phase is not None else entry


def test_two_phases_sharing_a_path_steer_clear_naming_both():
    assert steer_check("i", "r", ["a.py"], [_run("i", ["a.py"], "p1")], {}, phase="p2") == {
        "kind": "steer_clear",
        "initiative": "i",
        "other": "i",
        "phase": "p2",
        "other_phase": "p1",
        "paths": ["a.py"],
    }


def test_two_phases_sharing_no_path_give_none():
    assert steer_check("i", "r", ["a.py"], [_run("i", ["b.py"], "p1")], {}, phase="p2") is None


def test_same_initiative_entry_is_not_a_peer_without_two_different_phases():
    assert steer_check("i", "r", ["a.py"], [_run("i", ["a.py"], "p1")], {}, phase="p1") is None
    assert steer_check("i", "r", ["a.py"], [_run("i", ["a.py"])], {}, phase="p1") is None
    assert steer_check("i", "r", ["a.py"], [_run("i", ["a.py"], "p1")], {}) is None


def test_within_initiative_overlap_never_escalates():
    got = steer_check("i", "r", ["a.py"], [_run("i", ["a.py"], "p1")], {"i|i": ESCALATE_AT + 5}, phase="p2")
    assert got is not None and got["kind"] == "steer_clear"


def test_cross_initiative_overlap_at_the_streak_limit_escalates_even_behind_a_sibling():
    running = [_run("i", ["a.py"], "p1"), _run("j", ["a.py"])]
    assert steer_check("i", "r", ["a.py"], running, {"i|j": ESCALATE_AT}, phase="p2") == {
        "kind": "needs_chair",
        "initiative": "i",
        "other": "j",
        "cause": "steer_deferred",
        "reason": "i shares a.py with running j; the pair has been deferred three ticks",
    }


def test_call_without_phase_returns_the_old_shape_against_a_phased_entry():
    assert steer_check("i", "r", ["a.py"], [_run("j", ["a.py"], "p1")], {}) == {
        "kind": "steer_clear",
        "initiative": "i",
        "other": "j",
        "paths": ["a.py"],
    }


def test_cross_initiative_steer_clear_names_phases_when_the_candidate_has_one():
    got = steer_check("i", "r", ["a.py"], [_run("j", ["a.py"], "p1")], {}, phase="p2")
    assert got == {"kind": "steer_clear", "initiative": "i", "other": "j", "phase": "p2", "other_phase": "p1", "paths": ["a.py"]}
