from pathlib import Path

from agent_tools import chair_apply_widen
from agent_tools.queue_export import blob_hash, held_files, plan_export, read_baseline_record
from agent_tools.queue_rows import parse_item

REL = "work/i/p1/t1.md"
TICKET = "---\nstate: ready\ntitle: T\nsurfaces:\n- a.py\nid: t1\nphase: p1\n---\n\nBody.\n"
ACTION = {"kind": "widen_ticket", "initiative": "i", "task_id": "t1", "paths": ["b.py"], "additions": ["the helper"]}


class FakeStore:
    """One task row; `upserts` is every row written to it, a non-empty `refuses` turns each write away."""

    def __init__(self, refuses: str = "") -> None:
        self.row = parse_item("task", ("i", "p1", "t1.md"), TICKET)
        self.before = dict(self.row)
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
    ticket = tmp_path / REL
    ticket.parent.mkdir(parents=True)
    ticket.write_text(TICKET, encoding="utf-8")
    store = FakeStore(refuses)
    monkeypatch.setattr(chair_apply_widen.run_store, "read_queue", store.read_queue)
    monkeypatch.setattr(chair_apply_widen.run_store, "upsert_row_detail", store.upsert_row_detail)
    return store


def _held(tmp_path: Path, store: FakeStore) -> list:
    on_disk = {REL: (tmp_path / REL).read_text(encoding="utf-8")}
    writes, _ = plan_export([store.row], on_disk)
    return held_files(writes, on_disk, {path: {blob} for path, blob in read_baseline_record(tmp_path).items()})


def test_a_widen_with_runs_dir_puts_the_files_surfaces_body_and_hash_in_the_store_and_baseline(tmp_path, monkeypatch) -> None:
    store = _setup(tmp_path, monkeypatch)
    ok, reason = chair_apply_widen.apply_widen(tmp_path, ACTION, "2026-11-11", runs_dir=tmp_path / "runs")
    text = (tmp_path / REL).read_text(encoding="utf-8")
    parsed = parse_item("task", ("i", "p1", "t1.md"), text)
    assert (ok, reason) == (True, "widened t1: b.py")
    assert parsed["surfaces"] == ["a.py", "b.py"]
    assert (store.row["surfaces"], store.row["body"]) == (parsed["surfaces"], parsed["body"])
    assert read_baseline_record(tmp_path) == {REL: blob_hash(text)}
    assert _held(tmp_path, store) == []


def test_a_widen_without_runs_dir_leaves_the_store_and_baseline_alone(tmp_path, monkeypatch) -> None:
    store = _setup(tmp_path, monkeypatch)
    ok, reason = chair_apply_widen.apply_widen(tmp_path, ACTION, "2026-11-11")
    assert (ok, reason) == (True, "widened t1: b.py")
    assert (store.row, store.upserts) == (store.before, [])
    assert read_baseline_record(tmp_path) == {}
    assert [h.path for h in _held(tmp_path, store)] == [REL]


def test_a_store_refusal_is_reported_and_leaves_the_file_unrecorded_so_the_export_holds_it(tmp_path, monkeypatch) -> None:
    store = _setup(tmp_path, monkeypatch, refuses="exit 1")
    ok, reason = chair_apply_widen.apply_widen(tmp_path, ACTION, "2026-11-11", runs_dir=tmp_path / "runs")
    assert ok is True
    assert reason == "widened t1: b.py; the store did not take the widened t1: exit 1"
    assert read_baseline_record(tmp_path) == {}
    assert [h.path for h in _held(tmp_path, store)] == [REL]
