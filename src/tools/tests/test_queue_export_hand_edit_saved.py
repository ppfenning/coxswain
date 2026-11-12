"""A hand-edited file is saved under runs/.hand-edits and the store's export is written over it."""

from __future__ import annotations

import logging
from pathlib import Path

from agent_tools.queue_export import (
    Hashes,
    blob_hash,
    export_files,
    hand_edit_save_path,
    read_baseline_record,
    read_held_record,
)
from agent_tools.queue_rows import render_item, row_path

ROW = {
    "kind": "task", "initiative": "queue", "task_id": "codec", "phase": "p1", "state": "todo", "needs": [],
    "title": "Codec", "surfaces": [], "body": "One.\n", "extra": {},
}
PATH = row_path(ROW)
EXPORT = render_item(ROW)
HAND = EXPORT + "my own note\n"
SAVED_TO = f"runs/.hand-edits/{PATH.replace('/', '__')}.{blob_hash(HAND)[:12]}.md"


def _hashes() -> tuple[dict, Hashes]:
    stored: dict = {}
    return stored, Hashes(stored, lambda initiative, task_id, digest: stored.update({(initiative, task_id): digest}) or True)


def _hand_edited(workspace: Path) -> None:
    (workspace / PATH).parent.mkdir(parents=True)
    (workspace / PATH).write_text(HAND)


def test_the_save_path_turns_slashes_into_double_underscores_and_keeps_twelve_blob_characters() -> None:
    assert hand_edit_save_path("work/a/p1/t.md", "0123456789abcdef") == "runs/.hand-edits/work__a__p1__t.md.0123456789ab.md"


def test_an_edited_file_is_saved_then_overwritten_and_a_second_export_adds_nothing(tmp_path: Path) -> None:
    _hand_edited(tmp_path)
    stored, hashes = _hashes()
    touched, _ = export_files(tmp_path, [ROW], hashes=hashes)
    assert touched == [PATH]
    assert (tmp_path / PATH).read_text() == EXPORT
    assert (tmp_path / SAVED_TO).read_text() == HAND
    assert read_held_record(tmp_path) == [
        {"path": PATH, "initiative": "queue", "reason": read_held_record(tmp_path)[0]["reason"], "blob": blob_hash(HAND),
         "diff_summary": "body +1/-0 lines", "saved_to": SAVED_TO, "outcome": "saved"}
    ]
    assert read_baseline_record(tmp_path)[PATH] == blob_hash(EXPORT)
    assert len(stored) == 1
    export_files(tmp_path, [ROW], hashes=hashes)
    assert [p.name for p in (tmp_path / "runs/.hand-edits").iterdir()] == [Path(SAVED_TO).name]
    assert (tmp_path / PATH).read_text() == EXPORT


def test_the_same_hand_text_saved_twice_is_one_file(tmp_path: Path) -> None:
    _hand_edited(tmp_path)
    export_files(tmp_path, [ROW], hashes=_hashes()[1])
    (tmp_path / PATH).write_text(HAND)
    export_files(tmp_path, [ROW], hashes=_hashes()[1])
    assert [p.name for p in (tmp_path / "runs/.hand-edits").iterdir()] == [Path(SAVED_TO).name]


def test_a_save_that_cannot_be_written_leaves_the_hand_text_and_the_row_held(tmp_path: Path, caplog) -> None:
    _hand_edited(tmp_path)
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs/.hand-edits").write_text("not a directory\n")
    stored, hashes = _hashes()
    with caplog.at_level(logging.WARNING):
        touched, _ = export_files(tmp_path, [ROW], hashes=hashes)
    assert touched == []
    assert (tmp_path / PATH).read_text() == HAND
    [row] = read_held_record(tmp_path)
    assert (row["path"], row["blob"], row["saved_to"], row["outcome"]) == (PATH, blob_hash(HAND), "", "")
    assert any("could not save the hand edit" in record.getMessage() for record in caplog.records)
    assert stored == {}
