"""A task the store already holds as done is adopted, never quarantined, and never blocks a dependent phase."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import harness.epic as epic
import harness.store_read as read
from harness import work_mirror
from harness.store_migrate import open_store
from harness.store_write import Store, record_merge
from tests.test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    TASK_IDS,
    Runner,
    cart,
    drive,
    initiative,
    new_file_patch,
    repo,
)

INITIATIVE = "i1"
MISSING = "0" * 40


def _ctx(store_conn, root: Path, **more):
    fields = {
        "store": Store(store_conn), "initiative_id": INITIATIVE, "run_id": "run-1", "work_state": "store",
        "epoch": None, "cartridge": {}, "runner": None, "retry": epic._StoreRetry(), "repo": root,
        "items_lock": threading.Lock(), "launched": {}, "initiative_branch": None,
        "phase_branch": staticmethod(lambda phase: "main"), "scratch_branch": staticmethod(lambda task: f"scratch/{task}"),
        **more,
    }  # fmt: skip
    return type("Ctx", (), fields)()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git = ["git", "-C", str(root), "-c", "user.name=a", "-c", "user.email=a@b"]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "one"], check=True)
    return root


def _put(store: Store, initiative: str, item: dict, state: str, commit: str | None = None) -> None:
    """Upsert the row; a merge is stamped while it is approved, as in a run, and the row then moves on to `state`."""
    at = "2026-01-01T00:00:00+00:00"
    if commit is not None:
        epic._upsert_row(store, work_mirror.item_row(initiative, {**item, "state": "approved"}, at, "alice"))
        record_merge(store.conn, initiative, str(item["id"]), commit, at, "alice")
    epic._upsert_row(store, work_mirror.item_row(initiative, {**item, "state": state}, at, "alice"))


def _row(store: Store, initiative: str, task: str) -> dict:
    return {r["task_id"]: r for r in read.work_items(store.conn, initiative)}[task]


def _stale(task: str, phase: str = "p1", needs: tuple[str, ...] = ()) -> dict:
    """The item as this run holds it: approved with a merged commit that is on no branch."""
    return {
        "id": task,
        "phase": phase,
        "state": "approved",
        "needs": list(needs),
        "merged": True,
        "merged_commit": MISSING,
        "body": f"body of {task}",
    }


def test_a_task_done_in_the_store_is_adopted_not_quarantined(store_conn, tmp_path):
    ctx = _ctx(store_conn, _repo(tmp_path))
    items = [_stale("t1")]
    _put(ctx.store, INITIATIVE, items[0], "done", MISSING)
    before = _row(ctx.store, INITIATIVE, "t1")
    quarantined: list = []
    epic._quarantine_missing_merges(ctx, "p1", items, quarantined)
    assert quarantined == []
    assert (items[0]["state"], items[0]["merged"]) == ("done", True)
    assert _row(ctx.store, INITIATIVE, "t1") == before
    assert before["state"] == "done"


def test_a_row_still_approved_with_a_missing_commit_is_quarantined_as_before(store_conn, tmp_path):
    ctx = _ctx(store_conn, _repo(tmp_path))
    items = [_stale("t1")]
    _put(ctx.store, INITIATIVE, items[0], "approved", MISSING)
    quarantined: list = []
    epic._quarantine_missing_merges(ctx, "p1", items, quarantined)
    assert [q["id"] for q in quarantined] == ["t1"]
    assert quarantined[0]["reason"] == (
        f"merged commit {MISSING} is not on main; t1 was approved and merged by an earlier run and is not rebuilt"
    )
    assert _row(ctx.store, INITIATIVE, "t1")["state"] == "quarantined"


def test_a_store_read_error_leaves_the_quarantine_in_place(store_conn, tmp_path, monkeypatch):
    ctx = _ctx(store_conn, _repo(tmp_path))
    items = [_stale("t1")]
    _put(ctx.store, INITIATIVE, items[0], "done", MISSING)

    def boom(*_):
        raise RuntimeError("store down")

    monkeypatch.setattr(read, "work_items", boom)
    quarantined: list = []
    epic._quarantine_missing_merges(ctx, "p1", items, quarantined)
    assert [q["id"] for q in quarantined] == ["t1"]


def test_store_fresh_takes_only_done_rows_and_copies_its_input():
    items = [_stale("t1"), _stale("t2")]
    rows = [
        {"task_id": "t1", "state": "done", "merged": True, "merged_commit": "abc"},
        {"task_id": "t2", "state": "approved", "merged": True, "merged_commit": MISSING},
    ]
    fresh = epic._store_fresh(items, rows)
    assert [(i["state"], i["merged_commit"]) for i in fresh] == [("done", "abc"), ("approved", MISSING)]
    assert items == [_stale("t1"), _stale("t2")]
    assert epic._store_fresh(items, None) == items


def test_phase_one_done_from_another_machine_is_complete_and_phase_two_builds(repo, cart, tmp_path, monkeypatch):  # noqa: F811
    work = initiative()
    p1 = [i for i in work["items"] if i["phase"] == "p1-foundations"]
    store = Store(open_store("sqlite:///:memory:", "2026-01-01T00:00:00+00:00"))
    for item in p1:
        item["state"] = "approved"
        _put(store, work["id"], item, "approved", MISSING)
    real = epic._mirror_read

    def mirror_then_land(ctx, items):
        """The run reads its items, then another machine lands phase one."""
        real(ctx, items)
        for item in p1:
            _put(store, work["id"], item, "done")

    monkeypatch.setattr(epic, "_mirror_read", mirror_then_land)
    # The branch an earlier run made for phase one; its merged commits are on no branch this run can see.
    subprocess.run(["git", "-C", str(repo), "branch", "epic/demo-initiative/p1-foundations", "main"], check=True)
    runner = Runner({t: new_file_patch(f"{t}.txt") for t in TASK_IDS})
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=work, store=store, work_state="store")
    assert result["quarantined"] == []
    by_phase = {p["phase"]: p for p in result["phases"]}
    assert by_phase["p1-foundations"]["status"] == "complete"
    assert [t["id"] for t in result["tasks"]] == ["t3-cutover"]
    assert {r["task_id"]: r["state"] for r in read.work_items(store.conn, work["id"])}["t1-probe"] == "done"


def _lanes(ctx, items, p1_status, monkeypatch):
    """Run p1 then p2 through `_run_lanes`; every record goes through add_phase and `_record_phase`, as in a run."""
    launched: list[str] = []

    def fake_phase(_ctx, *, initiative, phase, parents, items, only):
        launched.append(phase)
        status = p1_status if phase == "p1" else "complete"
        return {
            "phase": phase,
            "status": status,
            "parents": list(parents),
            "awaiting_tasks": ["a1"] if status == "awaiting_land" else [],
            "task_records": [],
            "quarantined": [],
            "batch": [],
        }

    monkeypatch.setattr(epic, "_run_phase", fake_phase)
    phases: list = []

    def add_phase(record):
        phases.append(record)
        epic._record_phase(ctx, record)

    epic._run_lanes(
        ctx, initiative={"id": INITIATIVE}, items=items, parents={"p1": set(), "p2": {"p1"}}, waves=[["p1"], ["p2"]],
        add_phase=add_phase, phases=phases, tasks=[], quarantined=[], proposals=[], tally=epic._Tally(),
        headroom_usd=None, est_task_usd=0.0,
    )  # fmt: skip
    return launched, [(p["phase"], p["status"]) for p in phases]


def _stored_statuses(store_conn, phase: str) -> list[str]:
    mark = store_conn.dialect.placeholder
    sql = f"SELECT record_json FROM phases WHERE run_id = {mark} AND phase_id = {mark}"
    return [
        (r[0] if isinstance(r[0], dict) else json.loads(r[0]))["status"]
        for r in store_conn.query_all(sql, ("run-1", phase))
    ]


def _two_phases(store_conn, tmp_path, a1_state: str):
    ctx = _ctx(store_conn, _repo(tmp_path), max_parallel=2, land_at=epic.DEFAULT_LAND_AT)
    # Approved but unmerged in this run's items, so p2 can run only once the store says a1 is done.
    unmerged = {**_stale("a1"), "merged": False, "merged_commit": None}
    items = [unmerged, {"id": "b1", "phase": "p2", "state": "ready", "needs": ["a1"], "body": "b"}]
    _put(ctx.store, INITIATIVE, items[0], a1_state, MISSING)
    _put(ctx.store, INITIATIVE, items[1], "ready")
    return ctx, items


def test_a_parent_done_elsewhere_is_stored_complete_on_its_first_record_and_its_dependent_builds(
    store_conn, tmp_path, monkeypatch
):
    ctx, items = _two_phases(store_conn, tmp_path, "done")
    launched, phases = _lanes(ctx, items, "awaiting_land", monkeypatch)
    assert "p2" in launched
    assert [p for p in phases if p[0] == "p1"] == [("p1", "complete")]
    assert _stored_statuses(store_conn, "p1") == ["complete"]
    assert _row(ctx.store, INITIATIVE, "a1")["state"] == "done"


def test_a_parent_awaiting_land_that_is_not_done_in_the_store_keeps_its_record_and_blocks(
    store_conn, tmp_path, monkeypatch
):
    ctx, items = _two_phases(store_conn, tmp_path, "approved")
    launched, phases = _lanes(ctx, items, "awaiting_land", monkeypatch)
    assert launched == ["p1"]
    assert phases == [("p1", "awaiting_land"), ("p2", "blocked")]
    assert _stored_statuses(store_conn, "p1") == ["awaiting_land"]


def test_a_partial_parent_is_never_completed_from_the_store(store_conn, tmp_path, monkeypatch):
    ctx, items = _two_phases(store_conn, tmp_path, "done")
    launched, phases = _lanes(ctx, items, "partial", monkeypatch)
    assert launched == ["p1"]
    assert phases == [("p1", "partial"), ("p2", "blocked")]
    assert _stored_statuses(store_conn, "p1") == ["partial"]
