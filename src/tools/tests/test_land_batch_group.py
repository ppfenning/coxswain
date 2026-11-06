"""Grouping land sources into disjoint batches: one literal case per rule."""

from agent_tools.land_batch_group import batch_key, disjoint_batches, overlaps


def _src(initiative, files, repo="r", phase="p"):
    return {
        "initiative": initiative,
        "phase": phase,
        "run": f"run-{initiative}",
        "repo": repo,
        "files": files,
        "tasks": [],
    }


def test_three_disjoint_sources_form_one_batch():
    a, b, c = _src("a", ["x"]), _src("b", ["y"]), _src("c", ["z"])
    assert disjoint_batches([a, b, c], 3) == [[a, b, c]]


def test_overlapping_source_starts_its_own_batch():
    a, b, c = _src("a", ["x"]), _src("b", ["x"]), _src("c", ["z"])
    assert disjoint_batches([a, b, c], 3) == [[a, c], [b]]


def test_size_caps_a_batch():
    a, b, c = _src("a", ["x"]), _src("b", ["y"]), _src("c", ["z"])
    assert disjoint_batches([a, b, c], 2) == [[a, b], [c]]


def test_size_below_one_reads_as_one():
    a, b = _src("a", ["x"]), _src("b", ["y"])
    assert disjoint_batches([a, b], 0) == [[a], [b]]


def test_same_initiative_splits():
    a, b = _src("a", ["x"], phase="p1"), _src("a", ["y"], phase="p2")
    assert disjoint_batches([a, b], 5) == [[a], [b]]


def test_different_repos_split():
    a, b = _src("a", ["x"], repo="r1"), _src("b", ["y"], repo="r2")
    assert disjoint_batches([a, b], 5) == [[a], [b]]


def test_empty_files_source_is_alone():
    a, e, c = _src("a", ["x"]), _src("e", []), _src("c", ["z"])
    assert disjoint_batches([a, e, c], 5) == [[a, c], [e]]
    assert disjoint_batches([e, a], 5) == [[e], [a]]


def test_overlaps_returns_sorted_shared_paths():
    assert overlaps(_src("a", ["x", "a"]), _src("b", ["a", "x", "z"])) == ["a", "x"]


def test_batch_key_ignores_input_order():
    a, b = _src("a", ["x"]), _src("b", ["y"])
    assert batch_key([a, b]) == batch_key([b, a])
    assert len(batch_key([a, b])) == 10
