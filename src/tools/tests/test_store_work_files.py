"""The chair's work files rendered from the store's rows: what the export would write, without a file being opened."""
from pathlib import Path

from agent_tools import chair_read_quarantined
from agent_tools.queue_export import export_files, export_rows
from tests.test_queue_export import INITIATIVE_ROW, TASK_ROW

INTAKE_ROW = {"kind": "intake", "initiative": "intake", "task_id": "an-idea", "state": "queued", "title": "An idea",
              "body": "Do the thing.", "needs": [], "surfaces": [], "extra": {"repo": "~/repos/demo", "stage": "ready"}}


def test_the_rendered_files_are_exactly_what_the_export_writes_for_work_and_nothing_else(tmp_path: Path) -> None:
    rows = [TASK_ROW, INITIATIVE_ROW, INTAKE_ROW]
    rendered = chair_read_quarantined.store_work_files(tmp_path, rows)
    export_files(tmp_path, rows)
    on_disk = chair_read_quarantined.read_work_files(tmp_path)
    assert rendered == on_disk, "a reader handed either sees the same paths and the same text"
    assert list(rendered) == sorted(rendered) and all("work" in p.parts for p in rendered)
    assert len(rendered) == len([p for p in export_rows(rows) if p.startswith("work/")]) == 2, "the intake is not a work file"


def test_nothing_under_the_workspace_is_read_and_a_row_with_no_content_renders_nothing(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-workspace"
    rows = [TASK_ROW, {"kind": "task", "initiative": "i", "task_id": "t", "phase": "p", "state": "ready", "title": None,
                       "body": None, "extra": None}]
    rendered = chair_read_quarantined.store_work_files(missing, rows)
    assert len(rendered) == 1 and not missing.exists()
    assert chair_read_quarantined.store_work_files(missing, []) == {}
