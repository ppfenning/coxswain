"""Epic task builds lease pooled worktrees and record whether setup was warm or cold."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from test_epic_driver import cart, drive, git, initiative, repo  # noqa: F401 -- fixtures

import harness.epic as epic
from harness import pool_edge

ONE = ("t1-probe",)


def _work(name: str, *, tasks: tuple[str, ...] = ONE) -> dict:
    """One phase holding only `tasks`, under its own initiative id so each run cuts a fresh phase branch."""
    work = initiative(two_phases=False)
    return {**work, "id": name, "items": [i for i in work["items"] if i["id"] in tasks]}


def _setting(tmp_path: Path, *, keep: int = 4) -> epic.PoolSetting:
    """A pool whose environment sync appends a line to a counter file instead of installing anything."""
    syncs = tmp_path / "syncs.txt"
    code = "import sys; open(sys.argv[1], 'a').write('x\\n')"
    return epic.PoolSetting(
        pool_root=tmp_path / "pool", sync_argv=[sys.executable, "-c", code, str(syncs)], lock_names=["uv.lock"], keep=keep
    )


def _syncs(tmp_path: Path) -> int:
    counter = tmp_path / "syncs.txt"
    return len(counter.read_text().splitlines()) if counter.exists() else 0


def _entries(root: Path, setting: epic.PoolSetting) -> list[dict]:
    index = setting.pool_root / root.name / pool_edge.INDEX_NAME
    return json.loads(index.read_text())["entries"] if index.exists() else []


def _lock(root: Path, text: str) -> None:
    (root / "uv.lock").write_text(text, encoding="utf-8")
    git("add", "-A", cwd=root)
    git("commit", "-qm", f"lock {text}", cwd=root)


def _spy_builds(monkeypatch) -> list[tuple[dict, Path]]:
    """Every `_build_task` record with the worktree path the lane held when it returned."""
    seen: list[tuple[dict, Path]] = []
    real = epic._build_task

    def spy(ctx, **kw):
        record = real(ctx, **kw)
        seen.append((record, ctx.task_worktree(kw["phase"], kw["task"])))
        return record

    monkeypatch.setattr(epic, "_build_task", spy)
    return seen


def test_a_second_run_reuses_the_first_directory_with_no_second_sync(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    _lock(repo, "one")
    setting = _setting(tmp_path)
    builds = _spy_builds(monkeypatch)
    drive(repo, cart, tmp_path, work=_work("a"), run_id="epic-a", pool=setting)
    drive(repo, cart, tmp_path, work=_work("b"), run_id="epic-b", pool=setting)
    (first, first_path), (second, second_path) = builds
    assert second_path == first_path
    assert [first["setup"]["mode"], second["setup"]["mode"]] == ["cold", "warm"]
    assert _syncs(tmp_path) == 1
    assert any(str(second_path) in e["output"] for e in second["evidence"] if e["check"] == "patch_apply")


def test_a_changed_lockfile_between_builds_costs_exactly_one_sync(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    _lock(repo, "one")
    setting = _setting(tmp_path)
    builds = _spy_builds(monkeypatch)
    drive(repo, cart, tmp_path, work=_work("a"), run_id="epic-a", pool=setting)
    assert _syncs(tmp_path) == 1
    _lock(repo, "two")
    drive(repo, cart, tmp_path, work=_work("b"), run_id="epic-b", pool=setting)
    assert _syncs(tmp_path) == 2
    assert builds[1][0]["setup"]["mode"] == "warm"


def test_setup_seconds_are_never_negative(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    setting = _setting(tmp_path)
    builds = _spy_builds(monkeypatch)
    drive(repo, cart, tmp_path, work=_work("a"), run_id="epic-a", pool=setting)
    drive(repo, cart, tmp_path, work=_work("b"), run_id="epic-b", pool=setting)
    assert [r["setup"]["seconds"] >= 0 for r, _ in builds] == [True, True]


def test_the_epic_end_trims_idle_directories_to_the_keep_limit(repo, cart, tmp_path) -> None:  # noqa: F811
    setting = _setting(tmp_path, keep=1)
    work = _work("a", tasks=("t1-probe", "t2-bench"))
    drive(repo, cart, tmp_path, work=work, run_id="epic-a", pool=setting)
    entries = _entries(repo, setting)
    assert [e["leased"] for e in entries] == [False]
    assert [p.name for p in (setting.pool_root / repo.name).iterdir() if p.is_dir()] == [Path(entries[0]["path"]).name]


def test_the_pool_is_sized_from_the_lane_limit(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    keeps: list[int] = []
    real = pool_edge.acquire

    def spy(*args, **kw):
        keeps.append(kw["keep"])
        return real(*args, **kw)

    monkeypatch.setattr(pool_edge, "acquire", spy)
    drive(repo, cart, tmp_path, work=_work("a"), run_id="epic-a", pool=_setting(tmp_path))
    assert keeps == [4]


def test_a_kept_worktree_leaves_the_pool_and_its_entry_is_dropped(repo, cart, tmp_path) -> None:  # noqa: F811
    setting = _setting(tmp_path)
    drive(repo, cart, tmp_path, work=_work("a"), run_id="epic-keep", keep_worktrees=True, pool=setting)
    kept = Path(cart["landing_areas"]["worktree_root"]) / "_kept" / "epic-keep"
    assert (kept / "slot-1" / "t1-probe.txt").read_text().strip() == "ok"
    assert _entries(repo, setting) == []
    assert not (setting.pool_root / repo.name / "slot-1").exists()


def test_without_a_pool_the_build_is_cold_and_never_touches_the_pool(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    def refuse(*args, **kw):
        raise AssertionError("the pool was used with the setting off")

    monkeypatch.setattr(pool_edge, "acquire", refuse)
    builds = _spy_builds(monkeypatch)
    drive(repo, cart, tmp_path, work=_work("a"), run_id="epic-a")
    (record, path), = builds
    assert record["setup"]["mode"] == "cold"
    assert record["setup"]["seconds"] >= 0
    assert path == Path(cart["landing_areas"]["worktree_root"]) / "epic-a" / "p1-foundations" / "t1-probe"
    assert not (tmp_path / "pool").exists()
