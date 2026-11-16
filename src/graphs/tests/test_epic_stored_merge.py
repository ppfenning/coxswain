"""Under work_state: store a merge and its handoff are written to the row, and a later run resumes from them."""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from pathlib import Path

import harness.epic as epic
import harness.store_read as read
from core import workstore
from harness import work_mirror
from harness.handoff import plan_context
from harness.store_write import Store

INITIATIVE = "i1"
PATCH = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"


def _ctx(store_conn, repo: Path):
    fields = {
        "store": Store(store_conn), "initiative_id": INITIATIVE, "run_id": "run-1", "work_state": "store",
        "epoch": None, "cartridge": {}, "runner": None, "retry": epic._StoreRetry(), "repo": repo,
        "items_lock": threading.Lock(),
        "phase_branch": staticmethod(lambda phase: "main"), "scratch_branch": staticmethod(lambda task: f"scratch/{task}"),
    }  # fmt: skip
    return type("Ctx", (), fields)()


def _files(root: Path, *tasks: tuple[str, list[str]]) -> list[dict]:
    """Work items as a fresh run loads them: every file still says ready."""
    out = []
    for task, needs in tasks:
        path = root / f"{task}.md"
        path.write_text(f"---\nid: {task}\nstate: ready\n---\nbody of {task}\n")
        out.append(
            {"id": task, "phase": "p1", "state": "ready", "needs": needs, "path": str(path), "body": f"body of {task}"}
        )
    return out


def _put(ctx, item, state):
    row = work_mirror.item_row(INITIATIVE, {**item, "state": state}, "2026-01-01T00:00:00+00:00", "alice")
    epic._upsert_row(ctx.store, row)


def _repo(tmp_path: Path) -> tuple[Path, list[str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=a", "-c", "user.email=a@b"]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    commits = []
    for n in ("one", "two"):
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", n], check=True)
        head = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
        commits.append(head.stdout.strip())
    return repo, commits


def _merge_in_run_n(ctx, item, commit, monkeypatch):
    """The state_move slot of a task whose merge already made `commit`, as run N's batch executes it."""
    monkeypatch.setattr(epic, "_unkept_work", lambda *_: None)
    state = epic._Execution(
        landed={item["id"]: True}, merged={item["id"]: True}, moved={}, quarantined=[],
        commits={item["id"]: commit}, builds={item["id"]: {"patch": PATCH, "decisions": ["use b"]}},
    )  # fmt: skip
    move = {"kind": "state_move", "target": item["id"]}
    epic._execute(ctx, move, slot="state_move", subject=item["id"], phase="p1", state=state, by_id={item["id"]: item})
    return state


def _ready(items):
    return [i["id"] for i in workstore.ready_tasks(epic._ready_view(items), phase="p1")]


def test_a_parent_merged_in_run_n_lets_its_dependent_start_in_run_n_plus_1(store_conn, tmp_path, monkeypatch):
    repo, (first, _) = _repo(tmp_path)
    ctx = _ctx(store_conn, repo)
    run_n = _files(tmp_path, ("t1", []), ("t2", ["t1"]))
    for item in run_n:
        _put(ctx, item, "ready")
    assert _ready(run_n) == ["t1"]
    _merge_in_run_n(ctx, run_n[0], first, monkeypatch)
    row = {r["task_id"]: r for r in read.work_items(store_conn, INITIATIVE)}["t1"]
    assert (row["state"], row["merged_commit"], row["handoff"]["commit"]) == ("approved", first, first)
    run_n_plus_1 = _files(tmp_path, ("t1", []), ("t2", ["t1"]))
    epic._mirror_read(ctx, run_n_plus_1)
    assert _ready(run_n_plus_1) == ["t2"]
    assert run_n_plus_1[0]["handoff"]["files_changed"] == ["x.py"]


def test_a_restart_skips_merged_tasks_and_expects_their_commit_on_the_branch(store_conn, tmp_path, monkeypatch):
    repo, (first, second) = _repo(tmp_path)
    ctx = _ctx(store_conn, repo)
    items = _files(tmp_path, ("t1", []), ("t2", []))
    for item in items:
        _put(ctx, item, "ready")
        _merge_in_run_n(ctx, item, second if item["id"] == "t2" else first, monkeypatch)
    restart = _files(tmp_path, ("t1", []), ("t2", []))
    epic._mirror_read(ctx, restart)
    quarantined: list = []
    epic._quarantine_missing_merges(ctx, "p1", restart, quarantined)
    assert (_ready(restart), quarantined) == ([], [])
    assert [i["state"] for i in restart] == ["approved", "approved"]


def test_one_missing_commit_quarantines_that_task_and_leaves_the_others_merged(store_conn, tmp_path, monkeypatch):
    repo, (first, second) = _repo(tmp_path)
    ctx = _ctx(store_conn, repo)
    items = _files(tmp_path, ("t1", []), ("t2", []), ("t3", []))
    for item, commit in zip(items, [first, "0" * 40, second], strict=True):
        _put(ctx, item, "ready")
        _merge_in_run_n(ctx, item, commit, monkeypatch)
    before = {r["task_id"]: r for r in read.work_items(store_conn, INITIATIVE)}
    restart = _files(tmp_path, ("t1", []), ("t2", []), ("t3", []))
    epic._mirror_read(ctx, restart)
    quarantined: list = []
    epic._quarantine_missing_merges(ctx, "p1", restart, quarantined)
    after = {r["task_id"]: r for r in read.work_items(store_conn, INITIATIVE)}
    assert [q["id"] for q in quarantined] == ["t2"]
    assert f"merged commit {'0' * 40} is not on main" in quarantined[0]["reason"]
    assert after["t2"]["state"] == "quarantined"
    for kept in ("t1", "t3"):
        assert after[kept] == before[kept]
    assert _ready(restart) == []


def _twin_repo(tmp_path: Path) -> tuple[Path, str]:
    """main holds a rebased twin of the merged commit; the original sits only on side."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=a", "-c", "user.email=a@b"]

    def run(*args: str) -> str:
        return subprocess.run([*git, *args], check=True, capture_output=True, text=True).stdout.strip()

    run("init", "-q", "-b", "main")
    (repo / "x.py").write_text("a\n")
    run("add", "x.py")
    run("commit", "-q", "-m", "base")
    run("checkout", "-q", "-b", "side")
    (repo / "x.py").write_text("b\n")
    run("commit", "-q", "-am", "change x")
    merged = run("rev-parse", "HEAD")
    run("checkout", "-q", "main")
    (repo / "y.py").write_text("y\n")
    run("add", "y.py")
    run("commit", "-q", "-m", "new base")
    run("cherry-pick", merged)
    return repo, merged


def test_a_rebased_twin_of_the_merged_commit_is_not_quarantined(store_conn, tmp_path, monkeypatch):
    repo, merged = _twin_repo(tmp_path)
    ancestor = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", merged, "main"])
    assert ancestor.returncode != 0
    ctx = _ctx(store_conn, repo)
    (item,) = _files(tmp_path, ("t1", []))
    _put(ctx, item, "ready")
    _merge_in_run_n(ctx, item, merged, monkeypatch)
    before = {r["task_id"]: r for r in read.work_items(store_conn, INITIATIVE)}
    restart = _files(tmp_path, ("t1", []))
    epic._mirror_read(ctx, restart)
    quarantined: list = []
    epic._quarantine_missing_merges(ctx, "p1", restart, quarantined)
    after = {r["task_id"]: r for r in read.work_items(store_conn, INITIATIVE)}
    assert quarantined == []
    assert (restart[0]["state"], restart[0]["merged"]) == ("approved", True)
    assert after == before


def test_the_plan_input_holds_only_the_handoff_of_the_tasks_needed():
    handoffs = {
        "t1": {"files_changed": ["a.py"], "decisions": ["d1"], "branch": "scratch/t1", "commit": "c1"},
        "t9": {"files_changed": ["z.py"], "decisions": ["d9"], "branch": "scratch/t9", "commit": "c9"},
    }
    text = plan_context({"id": "t2", "body": "body of t2", "needs": ["t1"]}, handoffs)
    assert text.startswith("body of t2\n\n## Handoff from t1\nbranch: scratch/t1\ncommit: c1")
    assert "a.py" in text and "d1" in text
    assert "t9" not in text and "z.py" not in text


def _git(repo: Path, *args: str) -> str:
    git = ["git", "-C", str(repo), "-c", "user.name=a", "-c", "user.email=a@b"]
    return subprocess.run([*git, *args], check=True, capture_output=True, text=True).stdout.strip()


def _clone_behind_origin(tmp_path: Path) -> tuple[Path, Path, str]:
    """A clone whose main is one commit behind origin's main; the newest commit exists only on origin."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "commit", "-q", "--allow-empty", "-m", "one")
    clone = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    _git(origin, "commit", "-q", "--allow-empty", "-m", "two")
    return origin, clone, _git(origin, "rev-parse", "HEAD")


def _fault_ctx(store_conn, repo: Path):
    ctx = _ctx(store_conn, repo)
    ctx.merge_fetch_faults = set()
    return ctx


def _merged(ctx, tmp_path, monkeypatch, *pairs: tuple[str, str]) -> list[dict]:
    items = _files(tmp_path, *((task, []) for task, _ in pairs))
    for item, (_, commit) in zip(items, pairs, strict=True):
        _put(ctx, item, "ready")
        _merge_in_run_n(ctx, item, commit, monkeypatch)
    restart = _files(tmp_path, *((task, []) for task, _ in pairs))
    epic._mirror_read(ctx, restart)
    return restart


def _states(store_conn) -> dict[str, str]:
    return {r["task_id"]: r["state"] for r in read.work_items(store_conn, INITIATIVE)}


def test_a_merged_commit_only_on_origin_is_fetched_and_not_quarantined(store_conn, tmp_path, monkeypatch):
    _origin, repo, newest = _clone_behind_origin(tmp_path)
    ctx = _fault_ctx(store_conn, repo)
    restart = _merged(ctx, tmp_path, monkeypatch, ("t1", newest))
    quarantined: list = []
    epic._quarantine_missing_merges(ctx, "p1", restart, quarantined)
    assert quarantined == []
    assert (restart[0]["state"], restart[0]["merged"]) == ("approved", True)
    assert _states(store_conn) == {"t1": "approved"}


def test_a_fetch_that_fails_quarantines_nothing_and_raises_one_fault(store_conn, tmp_path, monkeypatch, caplog):
    origin, repo, _ = _clone_behind_origin(tmp_path)
    shutil.rmtree(origin)
    ctx = _fault_ctx(store_conn, repo)
    restart = _merged(ctx, tmp_path, monkeypatch, ("t1", "1" * 40), ("t2", "2" * 40))
    quarantined: list = []
    with caplog.at_level(logging.WARNING, logger=epic.__name__):
        epic._quarantine_missing_merges(ctx, "p1", restart, quarantined)
    assert quarantined == []
    assert [(i["state"], i["merged"]) for i in restart] == [("approved", True), ("approved", True)]
    assert _states(store_conn) == {"t1": "approved", "t2": "approved"}
    faults = [r.getMessage() for r in caplog.records if r.getMessage().startswith("harness fault:")]
    assert len(faults) == 1
    assert str(repo) in faults[0]
    assert str(origin) in faults[0]  # git's own error names the origin it could not read


def test_a_commit_absent_locally_and_on_origin_still_quarantines(store_conn, tmp_path, monkeypatch):
    _origin, repo, _ = _clone_behind_origin(tmp_path)
    ctx = _fault_ctx(store_conn, repo)
    restart = _merged(ctx, tmp_path, monkeypatch, ("t1", "0" * 40))
    quarantined: list = []
    epic._quarantine_missing_merges(ctx, "p1", restart, quarantined)
    assert [q["id"] for q in quarantined] == ["t1"]
    assert quarantined[0]["reason"].startswith(f"merged commit {'0' * 40} is not on main; t1 was approved and merged")
    assert ctx.merge_fetch_faults == set()
