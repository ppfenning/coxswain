"""The carry step: an approved merged task whose commit is off the phase branch is carried, marked done, rebuilt or left."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import harness.epic as epic
import harness.store_read as read
from harness.store_dialect import json_load
from harness.store_write import Store
from tests.test_epic_store_done import _put, _row

INITIATIVE = "i1"
PHASE_BRANCH = "epic/i1/p1"


def _run(repo: Path, *args: str) -> str:
    git = ["git", "-C", str(repo), "-c", "user.name=a", "-c", "user.email=a@b"]
    return subprocess.run([*git, *args], check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path: Path, squash: bool = False) -> tuple[Path, str]:
    """main with x.py; a side branch changes x.py and is --no-ff merged on a branch main never holds."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-q", "-b", "main")
    (repo / "x.py").write_text("a\n")
    _run(repo, "add", "x.py")
    _run(repo, "commit", "-q", "-m", "base")
    _run(repo, "checkout", "-q", "-b", "side")
    (repo / "x.py").write_text("b\n")
    _run(repo, "commit", "-q", "-am", "change x")
    _run(repo, "checkout", "-q", "-b", "merged", "main")
    _run(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    merge = _run(repo, "rev-parse", "HEAD")
    assert len(_run(repo, "rev-list", "--parents", "-n", "1", merge).split()) == 3
    _run(repo, "checkout", "-q", "main")
    if squash:
        (repo / "x.py").write_text("b\n")
        _run(repo, "commit", "-q", "-am", "change x (#7)")
    _run(repo, "branch", PHASE_BRANCH, "main")
    return repo, merge


def _ctx(store_conn, repo: Path, tmp_path: Path):
    worktree = tmp_path / "phase"
    _run(repo, "worktree", "add", "-q", str(worktree), PHASE_BRANCH)
    fields = {
        "store": Store(store_conn), "initiative_id": INITIATIVE, "run_id": "i1-2", "work_state": "store",
        "epoch": None, "cartridge": {}, "runner": None, "retry": epic._StoreRetry(), "repo": repo,
        "items_lock": threading.Lock(), "runs_dir": tmp_path / "runs", "default_ref": "main",
        "phase_branch": staticmethod(lambda phase: PHASE_BRANCH), "phase_worktree": staticmethod(lambda phase: worktree),
    }  # fmt: skip
    return type("Ctx", (), fields)()


def _item(commit: str, task: str = "t1") -> dict:
    return {
        "id": task,
        "phase": "p1",
        "state": "approved",
        "needs": [],
        "merged": True,
        "merged_commit": commit,
        "body": "b",
    }


def _stored(ctx, item) -> None:
    _put(ctx.store, INITIATIVE, item, "approved", item["merged_commit"])


def test_an_approved_task_from_an_earlier_run_is_carried_without_a_rebuild(store_conn, tmp_path, capsys):
    repo, merge = _repo(tmp_path)
    ctx, item = _ctx(store_conn, repo, tmp_path), _item(merge)
    _stored(ctx, item)
    epic._carry_missing_merges(ctx, "p1", [item])
    head = _run(repo, "rev-parse", PHASE_BRANCH)
    assert head != merge and (item["state"], item["merged"], item["merged_commit"]) == ("approved", True, head)
    assert item["carried_from"] == {"commit": merge, "fetched_from": "local"}
    assert _row(ctx.store, INITIATIVE, "t1")["merged_commit"] == head
    assert _run(repo, "show", f"{PHASE_BRANCH}:x.py") == "b"
    assert "carried" in capsys.readouterr().out


def test_a_task_whose_change_is_already_on_base_is_marked_done_with_its_pull_request(store_conn, tmp_path):
    repo, merge = _repo(tmp_path, squash=True)
    ctx, item = _ctx(store_conn, repo, tmp_path), _item(merge)
    _stored(ctx, item)
    epic._carry_missing_merges(ctx, "p1", [item])
    assert (item["state"], _row(ctx.store, INITIATIVE, "t1")["state"]) == ("done", "done")
    mark = store_conn.dialect.placeholder
    record = store_conn.query_one(f"SELECT record_json FROM task_records WHERE task_id = {mark}", ("t1",))
    assert json_load(record[0])["landed"]["pr"] == "7"


def test_an_unreachable_commit_goes_back_to_ready_and_is_not_quarantined(store_conn, tmp_path, monkeypatch, capsys):
    repo, _ = _repo(tmp_path)
    ctx, item = _ctx(store_conn, repo, tmp_path), _item("0" * 40)
    _stored(ctx, item)

    def refuse(*_a, **_k):
        raise AssertionError("quarantined for a missing merged commit")

    monkeypatch.setattr(epic, "_quarantine_task", refuse)
    epic._carry_missing_merges(ctx, "p1", [item])
    assert (item["state"], item["rebuild_reason"]) == ("ready", "commit not found")
    assert _row(ctx.store, INITIATIVE, "t1")["state"] == "ready"
    assert "commit not found" in capsys.readouterr().out


def test_a_task_claimed_by_an_active_land_is_left_untouched(store_conn, tmp_path, monkeypatch):
    repo, merge = _repo(tmp_path)
    ctx, item = _ctx(store_conn, repo, tmp_path), _item(merge)
    _stored(ctx, item)
    before, branch = _row(ctx.store, INITIATIVE, "t1"), _run(repo, "rev-parse", PHASE_BRANCH)
    import harness.land_claims as land_claims

    reads: list[str] = []
    monkeypatch.setattr(
        land_claims, "read_claimed_tasks", lambda conn, ini, now: reads.append(ini) or frozenset({"t1"})
    )
    epic._carry_missing_merges(ctx, "p1", [item])
    assert reads == [INITIATIVE]
    assert _row(ctx.store, INITIATIVE, "t1") == before and _run(repo, "rev-parse", PHASE_BRANCH) == branch
    assert (item["state"], item["merged_commit"]) == ("approved", merge) and "carried_from" not in item
    assert read.work_items(store_conn, INITIATIVE)[0]["state"] == "approved"


def test_a_refused_merge_write_after_the_pick_does_not_crash_and_the_next_start_does_not_pick_twice(
    store_conn, tmp_path, monkeypatch
):
    repo, merge = _repo(tmp_path)
    ctx, item = _ctx(store_conn, repo, tmp_path), _item(merge)
    _stored(ctx, item)

    def refuse(*_a, **_k):
        raise ValueError("cannot record a merge: state is 'done', not 'approved'")

    monkeypatch.setattr(epic, "record_merge", refuse)
    epic._carry_missing_merges(ctx, "p1", [item])
    head = _run(repo, "rev-parse", PHASE_BRANCH)
    assert head != merge and item["merged_commit"] == head
    assert _row(ctx.store, INITIATIVE, "t1")["merged_commit"] == merge
    monkeypatch.undo()
    restart = _item(merge)
    epic._carry_missing_merges(ctx, "p1", [restart])
    assert _run(repo, "rev-parse", PHASE_BRANCH) == head
    assert (restart["state"], restart["merged_commit"]) == ("approved", head)
    assert _row(ctx.store, INITIATIVE, "t1")["merged_commit"] == head


def test_a_done_row_is_not_carried_when_the_rows_cannot_be_read(store_conn, tmp_path, monkeypatch):
    repo, merge = _repo(tmp_path)
    ctx, item = _ctx(store_conn, repo, tmp_path), _item(merge)
    _put(ctx.store, INITIATIVE, item, "done", merge)
    branch = _run(repo, "rev-parse", PHASE_BRANCH)

    def boom(*_):
        raise RuntimeError("store down")

    monkeypatch.setattr(read, "work_items", boom)
    epic._carry_missing_merges(ctx, "p1", [item])
    assert _run(repo, "rev-parse", PHASE_BRANCH) == branch
    assert item["state"] == "approved"


def test_a_row_done_since_the_read_is_adopted_and_not_carried(store_conn, tmp_path, monkeypatch):
    repo, merge = _repo(tmp_path)
    ctx, item = _ctx(store_conn, repo, tmp_path), _item(merge)
    _stored(ctx, item)
    branch, real, calls = _run(repo, "rev-parse", PHASE_BRANCH), epic._read_store_rows, []

    def done_after_the_first_read(c):
        if calls:
            _put(c.store, INITIATIVE, item, "done", merge)
        calls.append(1)
        return real(c)

    monkeypatch.setattr(epic, "_read_store_rows", done_after_the_first_read)
    epic._carry_missing_merges(ctx, "p1", [item])
    assert _run(repo, "rev-parse", PHASE_BRANCH) == branch
    assert item["state"] == "done"


def test_a_failed_claims_read_adopts_done_rows_and_leaves_the_others(store_conn, tmp_path, monkeypatch):
    repo, merge = _repo(tmp_path)
    ctx = _ctx(store_conn, repo, tmp_path)
    done, open_ = _item(merge, "t1"), _item(merge, "t2")
    _put(ctx.store, INITIATIVE, done, "done", merge)
    _put(ctx.store, INITIATIVE, open_, "approved", merge)
    branch = _run(repo, "rev-parse", PHASE_BRANCH)
    import harness.land_claims as land_claims

    def boom(*_):
        raise RuntimeError("store down")

    monkeypatch.setattr(land_claims, "read_claimed_tasks", boom)
    epic._carry_missing_merges(ctx, "p1", [done, open_])
    assert (done["state"], open_["state"]) == ("done", "approved")
    assert _run(repo, "rev-parse", PHASE_BRANCH) == branch
