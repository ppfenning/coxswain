"""A worker's view of one initiative, from store rows: what goes in, and that the lane's own reader reads it back."""

from pathlib import Path

from agent_tools.worker_view import stub_files, stub_root, stub_rows, write_stub
from core import workstore


def _row(kind: str, initiative: str, task_id: str, phase: str = "", **fields) -> dict:
    return {"kind": kind, "initiative": initiative, "task_id": task_id, "phase": phase,
            "state": fields.pop("state", "ready" if kind == "task" else "todo"), "title": fields.pop("title", task_id),
            "needs": fields.pop("needs", []), "surfaces": [], "body": fields.pop("body", f"{task_id}\n"),
            "extra": fields.pop("extra", {})}


ROWS = [
    _row("initiative", "ship", "ship", extra={"repo": "~/src/app", "lane_model": "phase"}, body="Ship two notes.\n"),
    _row("task", "ship", "a", "p1"),
    _row("task", "ship", "b", "p2", needs=["a", "base"]),
    _row("task", "other", "base", "p1", state="done"),
    _row("task", "other", "unrelated", "p1"),
    _row("initiative", "other", "other"),
    _row("intake", "", "2026-11-21-ship"),
]


def test_the_view_holds_the_initiative_its_tasks_and_only_the_foreign_tasks_they_need():
    chosen = [(r["initiative"], r["task_id"]) for r in stub_rows(ROWS, "ship")]
    assert chosen == [("ship", "ship"), ("ship", "a"), ("ship", "b"), ("other", "base")]


def test_the_files_are_the_exports_own_paths_under_work():
    files = stub_files(ROWS, "ship")
    assert files is not None
    assert sorted(files) == ["work/other/p1/base.md", "work/ship/initiative.md", "work/ship/p1/a.md", "work/ship/p2/b.md"]
    assert "repo: ~/src/app" in files["work/ship/initiative.md"]


def test_no_initiative_row_is_no_view():
    assert stub_files([r for r in ROWS if r["task_id"] != "ship"], "ship") is None
    assert stub_files([], "ship") is None


def test_the_lanes_reader_reads_the_view_with_its_foreign_need_met(tmp_path: Path):
    root = stub_root(tmp_path, "ship.p2-1")
    files = stub_files(ROWS, "ship")
    assert files is not None
    write_stub(root, files)
    initiative = workstore.read_initiative(root / "work" / "ship")
    assert (initiative["id"], initiative["phases"]) == ("ship", ["p1", "p2"])
    assert [(i["id"], i["state"], i["needs"]) for i in initiative["items"]] == [("a", "ready", []), ("b", "ready", ["a", "base"])]
    assert initiative["foreign"] == {"base": "done"}
    assert root == tmp_path / "worker-launches" / "ship.p2-1.work"


def test_writing_the_view_again_gives_the_same_tree_and_drops_what_the_rows_no_longer_hold(tmp_path: Path):
    root = stub_root(tmp_path, "ship.p1-1")
    files = stub_files(ROWS, "ship")
    assert files is not None
    write_stub(root, {**files, "work/ship/p1/gone.md": "---\nstate: ready\n---\ngone\n"})
    write_stub(root, files)
    write_stub(root, files)
    found = {str(p.relative_to(root)): p.read_text(encoding="utf-8") for p in root.rglob("*.md")}
    assert found == files
