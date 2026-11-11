from dataclasses import replace

from agent_tools import chair_exec
from agent_tools.chair_exec import Deps, perform

REQUEUE = {"kind": "requeue_task", "initiative": "i", "phase": "p1", "task_id": "t1", "reason": "no run in 3 ticks", "epoch": 1}
NOTE = "No run in 3 ticks; rebuilding on current main."


class FakeStore:
    """One task row; `upserts` is every row written to it, `states` every set_state call."""

    def __init__(self, state: str, extra: dict | None = None, body: str = "\nBody.\n", upsert_fails: str = "") -> None:
        self.row = {"initiative": "i", "task_id": "t1", "state": state, "extra": extra or {}, "body": body}
        self.upsert_fails = upsert_fails
        self.upserts: list = []
        self.states: list = []

    def read_queue(self, runs_dir, initiative=None, kind=None):
        return [dict(self.row)]

    def upsert_row_detail(self, runs_dir, row) -> str:
        self.upserts.append(row)
        if self.upsert_fails:
            return self.upsert_fails
        self.row = {**self.row, "extra": row["extra"], "body": row["body"]}
        return ""

    def set_state(self, *args, **kwargs):
        self.states.append(args)
        return self.result


def _run(tmp_path, monkeypatch, store: FakeStore, result, rows: list, epoch: int = 1) -> list:
    store.result = result
    monkeypatch.setattr(chair_exec.run_store, "read_queue", store.read_queue)
    monkeypatch.setattr(chair_exec.run_store, "upsert_row_detail", store.upsert_row_detail)
    monkeypatch.setattr(chair_exec.store_cli, "set_state", store.set_state)
    deps = Deps(
        run=lambda argv: (0, ""),
        delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "",
        record=rows.append,
        run_id=lambda action: "run-1",
        repo_for=lambda action: "r",
        rev_parse=lambda repo, ref: None,
    )
    return perform([REQUEUE], replace(deps, runs_dir=tmp_path / "runs"), lambda: epoch, False)


def test_an_approved_task_becomes_ready_with_its_reason_stored_on_the_row(tmp_path, monkeypatch) -> None:
    store, rows = FakeStore("approved", {"keep": 1}), []
    results = _run(tmp_path, monkeypatch, store, chair_exec.store_cli.StateSet({}), rows)
    assert store.row["extra"] == {"keep": 1, "land_conflict": "no run in 3 ticks"}
    assert store.row["body"] == f"\nBody.\n\n{NOTE}\n"
    assert store.states == [(tmp_path / "runs", "i", "t1", "ready", "chair-loop", "approved")]
    assert (results[0]["status"], results[0]["reason"]) == ("done", "t1 requeued: no run in 3 ticks")
    assert rows == [{**REQUEUE, "status": "done", "reason": "t1 requeued: no run in 3 ticks"}]


def test_a_task_already_done_in_the_store_is_left_alone_and_recorded_skipped(tmp_path, monkeypatch) -> None:
    store, rows = FakeStore("done"), []
    results = _run(tmp_path, monkeypatch, store, chair_exec.store_cli.StateSet({}), rows)
    assert (store.upserts, store.states) == ([], [])
    assert (results[0]["status"], results[0]["reason"]) == ("skipped", "t1 is done, not approved")
    assert [(r["kind"], r["status"]) for r in rows] == [("requeue_task", "skipped")]


def test_a_land_that_completes_after_the_row_read_is_skipped_and_the_row_is_put_back(tmp_path, monkeypatch) -> None:
    store, rows = FakeStore("approved", {"keep": 1}), []
    results = _run(tmp_path, monkeypatch, store, chair_exec.store_cli.StateRefused("state is done", "done"), rows)
    assert store.row["extra"] == {"keep": 1}
    assert store.row["body"] == "\nBody.\n"
    assert (results[0]["status"], results[0]["reason"]) == ("skipped", "t1 is done, not approved")
    assert [(r["kind"], r["status"]) for r in rows] == [("requeue_task", "skipped")]


def test_a_refusal_with_no_state_is_failed_and_the_row_is_put_back(tmp_path, monkeypatch) -> None:
    store, rows = FakeStore("approved"), []
    results = _run(tmp_path, monkeypatch, store, chair_exec.store_cli.StateRefused("no such row"), rows)
    assert (store.row["extra"], store.row["body"]) == ({}, "\nBody.\n")
    assert results[0]["status"] == "failed"
    assert [(r["kind"], r["status"]) for r in rows] == [("requeue_task", "failed")]


def test_a_row_the_store_will_not_take_fails_the_action_before_any_state_change(tmp_path, monkeypatch) -> None:
    store, rows = FakeStore("approved", upsert_fails="exit 2"), []
    results = _run(tmp_path, monkeypatch, store, chair_exec.store_cli.StateSet({}), rows)
    assert store.states == []
    assert (results[0]["status"], results[0]["reason"]) == ("failed", "the store did not take the reason of t1: exit 2")


def test_a_stale_epoch_writes_nothing_and_is_recorded_fenced(tmp_path, monkeypatch) -> None:
    store, rows = FakeStore("approved"), []
    results = _run(tmp_path, monkeypatch, store, chair_exec.store_cli.StateSet({}), rows, epoch=2)
    assert (store.upserts, store.states) == ([], [])
    assert [(r["kind"], r["status"]) for r in rows] == [("requeue_task", "fenced")]
    assert results[0]["status"] == "fenced"
