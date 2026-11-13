"""Cartridges are published to the store under the digest of their files and written back exactly."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_tools import cartridge_snapshot as cs


def tree(root: Path, **files: bytes) -> Path:
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def test_the_sha_follows_the_path_the_bytes_and_the_executable_bit() -> None:
    base = {"team/cartridge.yaml": cs.entry(b"k: v\n", False)}
    assert cs.sha_of(base) == cs.sha_of(dict(base))
    assert cs.sha_of(base) != cs.sha_of({"team/cartridge.yaml": cs.entry(b"k: w\n", False)})
    assert cs.sha_of(base) != cs.sha_of({"other/cartridge.yaml": cs.entry(b"k: v\n", False)})
    assert cs.sha_of(base) != cs.sha_of({"team/cartridge.yaml": cs.entry(b"k: v\n", True)})


def test_bytes_that_are_not_text_survive_the_snapshot() -> None:
    held = cs.entry(b"\xff\x00\xfe", False)
    assert "b64" in held and cs.data_of(held) == b"\xff\x00\xfe"
    assert cs.entry(b"plain", True) == {"text": "plain", "x": True}


def test_the_write_plan_names_only_what_differs() -> None:
    files = {"a": cs.entry(b"1", False), "b": cs.entry(b"2", False)}
    on_disk = {"a": cs.entry(b"1", False), "b": cs.entry(b"old", False), "gone": cs.entry(b"x", False)}
    assert cs.plan_write(files, on_disk) == (["b"], ["gone"])
    assert cs.plan_write(files, files) == ([], [])


def test_reading_a_directory_skips_version_control_and_caches(tmp_path: Path) -> None:
    root = tree(tmp_path / "cartridges", **{"team/cartridge.yaml": b"k: v\n", ".git/HEAD": b"ref", "team/__pycache__/x.pyc": b"\x00"})
    assert sorted(cs.read_tree(root)) == ["team/cartridge.yaml"]


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from harness.store_dialect import connect
    from harness.store_migrate import default_modules, migrate

    conn = connect(f"sqlite:///{tmp_path / 'store.db'}")
    migrate(conn, "2026-11-12T00:00:00Z", default_modules())
    monkeypatch.setattr(cs, "_conn", lambda runs_dir: conn)
    yield tmp_path / "ws" / "runs"
    conn.close()


def test_a_lane_ends_with_exactly_the_published_files(tmp_path: Path, store: Path) -> None:
    source = tree(tmp_path / "chair", **{"team/cartridge.yaml": b"k: v\n", "team/hooks/check.sh": b"#!/bin/sh\n"})
    (source / "team/hooks/check.sh").chmod(0o755)
    sha = cs.publish(store, source, by="chair-host")
    assert cs.publish(store, source, by="chair-host") == sha, "the same files are the same snapshot"
    lane = tree(tmp_path / "lane", **{"team/cartridge.yaml": b"k: old\n", "team/stale/left.md": b"stale\n"})
    with pytest.raises(PermissionError, match="no fetch put there"):
        cs.fetch(store, sha, lane)
    assert (lane / "team/cartridge.yaml").read_bytes() == b"k: old\n", "a person's files are not replaced unasked"
    assert cs.fetch(store, sha, lane, replace=True) == (2, 1)
    assert cs.read_tree(lane) == cs.read_tree(source)
    assert not (lane / "team/stale").exists(), "a directory the snapshot emptied is removed"
    assert (lane / "team/hooks/check.sh").stat().st_mode & 0o111
    assert cs.fetch(store, sha, lane) == (0, 0), "a lane that already matches is not rewritten"


def test_a_changed_cartridge_is_a_new_snapshot_and_the_old_one_stays(tmp_path: Path, store: Path) -> None:
    source = tree(tmp_path / "chair", **{"team/cartridge.yaml": b"k: v\n"})
    first = cs.publish(store, source)
    (source / "team/cartridge.yaml").write_bytes(b"k: w\n")
    second = cs.publish(store, source)
    assert first != second
    lane = tmp_path / "lane"
    cs.fetch(store, first, lane)
    assert (lane / "team/cartridge.yaml").read_bytes() == b"k: v\n", "a run pinned to the older sha still gets its files"


def test_an_unknown_sha_and_an_empty_directory_are_refused(tmp_path: Path, store: Path) -> None:
    with pytest.raises(LookupError, match="holds no cartridge snapshot"):
        cs.fetch(store, "0" * 64, tmp_path / "lane")
    assert not (tmp_path / "lane").exists()
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError, match="holds no cartridge files"):
        cs.publish(store, tmp_path / "empty")


def test_a_fetch_follows_a_newer_snapshot_over_its_own_last_write_but_not_over_an_edit(tmp_path: Path, store: Path) -> None:
    source = tree(tmp_path / "chair", **{"team/cartridge.yaml": b"k: v\n"})
    first = cs.publish(store, source)
    lane = tmp_path / "lane"
    cs.fetch(store, first, lane)
    (source / "team/cartridge.yaml").write_bytes(b"k: w\n")
    second = cs.publish(store, source)
    assert cs.fetch(store, second, lane) == (1, 0), "what the last fetch left is the system's own copy"
    (lane / "team/cartridge.yaml").write_bytes(b"k: mine\n")
    with pytest.raises(PermissionError):
        cs.fetch(store, first, lane)
    assert cs.may_replace(None, None) and cs.may_replace("a", "a") and not cs.may_replace("a", "b") and not cs.may_replace("a", None)


def test_a_link_is_kept_as_a_link(tmp_path: Path, store: Path) -> None:
    source = tree(tmp_path / "chair", **{"team/cartridge.yaml": b"k: v\n"})
    (source / "base").symlink_to("../shared/base")
    sha = cs.publish(store, source)
    lane = tmp_path / "lane"
    cs.fetch(store, sha, lane)
    assert (lane / "base").is_symlink() and str((lane / "base").readlink()) == "../shared/base"


def _profile(tmp_path: Path, git: bool) -> dict:
    return {"workspace_dir": str(tmp_path / "ws"), "chair": {"workspace_git": git}}


def test_the_chair_publishes_only_when_lanes_read_the_store(tmp_path: Path, store: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    from agent_tools import cli, courier_bus

    runs_dir = tmp_path / "ws" / "runs"
    tree(tmp_path / "ws" / "cartridges", **{"team/cartridge.yaml": b"k: v\n"})
    runs_dir.mkdir(parents=True)
    monkeypatch.setattr(courier_bus, "uses_store", lambda path: True)
    sha = cli._published_cartridges(runs_dir, _profile(tmp_path, git=False))
    assert sha == cs.sha_of(cs.read_tree(tmp_path / "ws" / "cartridges"))
    assert cli._published_cartridges(runs_dir, _profile(tmp_path, git=True)) is None, "a workspace in git keeps the copy"
    monkeypatch.setattr(courier_bus, "uses_store", lambda path: False)
    assert cli._published_cartridges(runs_dir, _profile(tmp_path, git=False)) is None, "a store only this machine has cannot serve a lane"
    monkeypatch.setattr(courier_bus, "uses_store", lambda path: True)
    monkeypatch.setattr(cs, "publish", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("store down")))
    assert cli._published_cartridges(runs_dir, _profile(tmp_path, git=False)) is None
    assert "copying them instead" in capsys.readouterr().out
