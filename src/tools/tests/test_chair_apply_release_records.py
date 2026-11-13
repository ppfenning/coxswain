"""`apply_release` with `runs_dir` carries its note to the store row and the export baseline; without it, it touches neither."""
from pathlib import Path

from agent_tools import chair_apply_release
from agent_tools.chair_apply_release import _note, _release_row, apply_release, release_ticket
from agent_tools.queue_export import HELD_REASON, held_files, plan_export, read_baseline_record
from agent_tools.queue_rows import render_item, row_path

DAY = "2026-11-09"
NOTE = "Released from approval on 2026-11-09: stuck. Rebuild on the current main."
ROW = {
    "kind": "task", "initiative": "epic-a", "task_id": "t1", "phase": "p1", "state": "approved", "needs": [],
    "title": "Land by task", "surfaces": ["a.py"], "body": "Do the thing.\n", "extra": {"id": "t1", "phase": "p1"},
}
PATH = row_path(ROW)


class FakeStore:
    """One stored task row; `upserts` is every partial row written, merged the way the store merges one."""

    def __init__(self, refuses: str = "") -> None:
        self.row = dict(ROW)
        self.refuses = refuses
        self.upserts: list = []

    def read_queue(self, runs_dir, initiative=None, kind=None):
        return [dict(self.row)]

    def upsert_row_detail(self, runs_dir, row) -> str:
        self.upserts.append(row)
        if self.refuses:
            return self.refuses
        self.row = {**self.row, **row}
        return ""


def _setup(tmp_path: Path, monkeypatch, refuses: str = "") -> FakeStore:
    store = FakeStore(refuses)
    monkeypatch.setattr(chair_apply_release.run_store, "read_queue", store.read_queue)
    monkeypatch.setattr(chair_apply_release.run_store, "upsert_row_detail", store.upsert_row_detail)
    (tmp_path / PATH).parent.mkdir(parents=True)
    (tmp_path / PATH).write_text(render_item(ROW), encoding="utf-8")
    return store


def _held(tmp_path: Path, store: FakeStore, baselines: dict) -> list:
    on_disk = {PATH: (tmp_path / PATH).read_text(encoding="utf-8")}
    writes, _ = plan_export([store.row], on_disk)
    return held_files(writes, on_disk, baselines)


def test_release_row_adds_the_note_once_and_never_carries_state() -> None:
    once = _release_row(ROW, NOTE)
    assert once == {"initiative": "epic-a", "task_id": "t1", "body": f"Do the thing.\n\n{NOTE}\n"}
    assert _release_row({**ROW, "body": once["body"]}, NOTE) == once


def test_release_with_runs_dir_updates_the_store_body_and_the_baseline(tmp_path, monkeypatch) -> None:
    store = _setup(tmp_path, monkeypatch)
    ok, message = apply_release(tmp_path, "epic-a", ["t1"], "stuck", DAY, runs_dir=tmp_path / "runs")
    assert (ok, message) == (True, "released t1")
    assert store.row["body"].endswith(f"\n\n{NOTE}\n")
    assert store.row["state"] == "approved"
    text = (tmp_path / PATH).read_text(encoding="utf-8")
    assert text.endswith(f"\n\n{NOTE}\n") and "state: ready" in text
    record = read_baseline_record(tmp_path)
    assert list(record) == [PATH]
    assert _held(tmp_path, store, {PATH: [record[PATH]]}) == []
    assert [h.reason for h in _held(tmp_path, store, {})] == [HELD_REASON]  # without the record the note is a hand edit


def test_once_the_executor_mirrors_state_the_export_has_nothing_to_write(tmp_path, monkeypatch) -> None:
    store = _setup(tmp_path, monkeypatch)
    apply_release(tmp_path, "epic-a", ["t1"], "stuck", DAY, runs_dir=tmp_path / "runs")
    store.row = {**store.row, "state": "ready"}  # what the executor's store_cli.mirror_state does next
    on_disk = {PATH: (tmp_path / PATH).read_text(encoding="utf-8")}
    assert plan_export([store.row], on_disk) == ([], [])


def test_the_note_text_is_pinned() -> None:
    assert _note("stuck.", DAY) == NOTE
    new, _ = release_ticket("---\nstate: approved\n---\nBody.\n", "stuck.", DAY)
    assert new == f"---\nstate: ready\n---\nBody.\n\n{NOTE}\n"


def test_a_second_release_does_not_duplicate_the_note(tmp_path, monkeypatch) -> None:
    store = _setup(tmp_path, monkeypatch)
    apply_release(tmp_path, "epic-a", ["t1"], "stuck", DAY, runs_dir=tmp_path / "runs")
    apply_release(tmp_path, "epic-a", ["t1"], "stuck", DAY, runs_dir=tmp_path / "runs")
    assert store.row["body"].count(NOTE) == 1
    assert len(store.upserts) == 1  # the ready file did not change, so nothing went to the store


def test_a_store_refusal_is_in_the_message_and_the_file_is_not_baselined(tmp_path, monkeypatch) -> None:
    store = _setup(tmp_path, monkeypatch, refuses="exit 1")
    ok, message = apply_release(tmp_path, "epic-a", ["t1"], "stuck", DAY, runs_dir=tmp_path / "runs")
    assert ok is True
    assert message == "released t1; the store did not take the note of t1: exit 1; the file is not baselined"
    assert read_baseline_record(tmp_path) == {}
    assert [h.reason for h in _held(tmp_path, store, {})] == [HELD_REASON]  # held and visible, not overwritten


def test_a_missing_store_row_is_reported_and_the_file_is_not_baselined(tmp_path, monkeypatch) -> None:
    store = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(chair_apply_release.run_store, "read_queue", lambda runs_dir, initiative=None, kind=None: [])
    ok, message = apply_release(tmp_path, "epic-a", ["t1"], "stuck", DAY, runs_dir=tmp_path / "runs")
    assert (ok, message) == (True, "released t1; the store did not take the note of t1: no store row; the file is not baselined")
    assert store.upserts == [] and read_baseline_record(tmp_path) == {}


def test_an_unavailable_real_store_is_reported_and_the_file_is_not_baselined(tmp_path, monkeypatch) -> None:
    """No fake: the real `run_store.read_queue` answers [] when the harness python is missing."""
    monkeypatch.setattr(chair_apply_release.run_store, "_harness_python", lambda: None)
    (tmp_path / PATH).parent.mkdir(parents=True)
    (tmp_path / PATH).write_text(render_item(ROW), encoding="utf-8")
    ok, message = apply_release(tmp_path, "epic-a", ["t1"], "stuck", DAY, runs_dir=tmp_path / "runs")
    assert ok is True and "no store row" in message
    assert read_baseline_record(tmp_path) == {}
    assert (tmp_path / PATH).read_text(encoding="utf-8").endswith(f"\n\n{NOTE}\n")


def test_release_without_runs_dir_leaves_the_store_and_the_baseline_alone(tmp_path, monkeypatch) -> None:
    store = _setup(tmp_path, monkeypatch)
    assert apply_release(tmp_path, "epic-a", ["t1"], "stuck", DAY) == (True, "released t1")
    assert store.upserts == [] and store.row == ROW
    assert read_baseline_record(tmp_path) == {}
