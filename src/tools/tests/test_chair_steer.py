from agent_tools.chair_steer import shared_paths, steer_check, steer_heads


def _clear(initiative: str, other: str) -> dict:
    return {"kind": "steer_clear", "initiative": initiative, "other": other, "paths": ["f"]}


def _deferred(initiative: str, other: str) -> dict:
    return {"kind": "needs_chair", "initiative": initiative, "other": other, "cause": "steer_deferred", "reason": "r"}


def _run(id: str, surfaces: list[str], repo: str = "r") -> dict:
    return {"id": id, "repo": repo, "surfaces": surfaces}


def test_equal_files_overlap():
    assert shared_paths(["a.py"], ["a.py"]) == ["a.py"]


def test_disjoint_files_return_none():
    assert steer_check("cand", "r", ["a.py"], [_run("peer", ["b.py"])], {}) is None


def test_directory_overlaps_file_and_reports_file():
    assert shared_paths(["src/ui/"], ["src/ui/regatta.rs"]) == ["src/ui/regatta.rs"]


def test_snapshots_ignored():
    assert shared_paths(["src/ui/snapshots/"], ["src/ui/snapshots/a.snap"]) == []


def test_snapshots_mid_path_ignored():
    assert shared_paths(["a/snapshots/b/c.rs"], ["a/"]) == []


def test_lock_file_ignored():
    assert shared_paths(["Cargo.lock"], ["Cargo.lock"]) == []


def test_package_lock_ignored():
    assert shared_paths(["web/package-lock.json"], ["web/"]) == []


def test_new_marker_stripped():
    assert shared_paths(["a.py (new)"], ["a.py"]) == ["a.py"]


def test_other_repo_skipped():
    assert steer_check("cand", "r", ["a.py"], [_run("peer", ["a.py"], repo="s")], {}) is None


def test_own_id_skipped():
    assert steer_check("cand", "r", ["a.py"], [_run("cand", ["a.py"])], {}) is None


def test_first_overlap_in_order_wins():
    running = [_run("none", ["z.py"]), _run("first", ["a.py"]), _run("second", ["a.py"])]
    assert steer_check("cand", "r", ["a.py"], running, {})["other"] == "first"


def test_streak_two_steers_clear():
    action = steer_check("cand", "r", ["a.py"], [_run("peer", ["a.py"])], {"cand|peer": 2})
    assert action == {"kind": "steer_clear", "initiative": "cand", "other": "peer", "paths": ["a.py"]}


def test_streak_three_needs_chair():
    action = steer_check("cand", "r", ["src/x.rs"], [_run("peer", ["src/x.rs"])], {"cand|peer": 3})
    assert action == {
        "kind": "needs_chair",
        "initiative": "cand",
        "other": "peer",
        "cause": "steer_deferred",
        "reason": "cand shares src/x.rs with running peer; the pair has been deferred three ticks",
    }


def test_a_steer_clear_names_its_other_as_the_head():
    assert steer_heads([_clear("b", "a")]) == ["a"]


def test_a_steer_deferred_needs_chair_names_its_other_as_the_head():
    assert steer_heads([_deferred("b", "a")]) == ["a"]


def test_an_other_that_is_itself_deferred_is_not_a_head():
    assert steer_heads([_clear("c", "b"), _clear("b", "a")]) == ["a"]


def test_two_deferred_initiatives_naming_one_head_give_it_once():
    assert steer_heads([_clear("b", "a"), _deferred("c", "a")]) == ["a"]


def test_heads_come_first_seen_first():
    assert steer_heads([_clear("b", "z"), _clear("c", "a")]) == ["z", "a"]


def test_a_self_pair_and_other_kinds_give_no_head():
    other_kinds = [{"kind": "launch_epic", "initiative": "a", "other": "x"}, {"kind": "needs_chair", "initiative": "d", "cause": "no_run", "other": "y"}]
    assert steer_heads([_clear("i", "i"), *other_kinds]) == []
