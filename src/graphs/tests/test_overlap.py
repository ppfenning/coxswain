from __future__ import annotations

from harness.overlap import serialise, shared_paths


def test_exact_path_collision() -> None:
    assert shared_paths(["src/x.py", "src/y.py"], ["src/x.py"]) == ["src/x.py"]


def test_new_marker_and_space_are_stripped() -> None:
    assert shared_paths([" src/x.py (new)"], ["src/x.py"]) == ["src/x.py"]


def test_directory_prefix_collision_reports_longer_path() -> None:
    assert shared_paths(["src/a"], ["src/a/b.py"]) == ["src/a/b.py"]


def test_prefix_is_segment_safe() -> None:
    assert shared_paths(["src/a"], ["src/ab.py"]) == []


def test_collision_with_running_phase() -> None:
    chosen, held = serialise(["b"], {"b": ["src/x.py"]}, {"a": ["src/x.py"]})
    assert chosen == []
    assert list(held) == ["b"]


def test_earlier_candidate_wins() -> None:
    chosen, held = serialise(
        ["a", "b", "c"],
        {"a": ["src/x.py"], "b": ["src/x.py"], "c": []},
        {},
    )
    assert chosen == ["a", "c"]
    assert list(held) == ["b"]


def test_reason_wording() -> None:
    _, held = serialise(["a", "b"], {"a": ["src/x.py"], "b": ["src/x.py"]}, {})
    assert held == {"b": "phase 'b' waits for phase 'a': both declare src/x.py"}


def test_phase_without_surfaces_never_collides() -> None:
    chosen, held = serialise(["a", "b"], {"a": ["src/x.py"]}, {"r": ["src/x.py"]})
    assert chosen == ["b"]
    assert list(held) == ["a"]
