"""Insta snapshots count as inside the surfaces only when their module is a surface."""

from __future__ import annotations

from graphs.delivery.snapshot_surfaces import drop_pending_snapshots, is_surface_snapshot, snapshot_module_paths

REGATTA = "src/ui/snapshots/coxtop__ui__regatta__tests__x.snap"


def test_a_snapshot_of_a_surface_module_is_inside() -> None:
    assert is_surface_snapshot(REGATTA, {"src/ui/regatta.rs"})


def test_a_snapshot_of_another_module_is_outside() -> None:
    assert not is_surface_snapshot(REGATTA, {"src/ui/feed.rs"})


def test_the_directorys_own_module_snapshot_names_mod_rs() -> None:
    path = "src/ui/snapshots/coxtop__ui__tests__x.snap"
    assert snapshot_module_paths(path) == ["src/ui/mod.rs"]
    assert is_surface_snapshot(path, {"src/ui/mod.rs"})


def test_a_pending_snapshot_is_dropped_whether_or_not_its_module_is_a_surface() -> None:
    touched = [REGATTA + ".new", "src/ui/regatta.rs"]
    assert drop_pending_snapshots(touched) == ["src/ui/regatta.rs"]


def test_a_snap_that_names_no_module_is_not_a_surface_snapshot() -> None:
    assert not is_surface_snapshot("src/ui/snapshots/a.snap", {"src/ui/a.rs"})
    assert snapshot_module_paths("src/ui/snapshots/a.snap") == []
