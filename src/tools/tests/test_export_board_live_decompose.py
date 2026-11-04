import os
import subprocess
import sys
from pathlib import Path

from agent_tools import cli, run_store, store_fill

TICKET = "---\nstate: todo\ntitle: Held\n---\nBody.\n"
ANCHOR = {
    "kind": "task",
    "initiative": "anchor",
    "task_id": "kept",
    "phase": "p",
    "state": "todo",
    "needs": [],
    "title": "Kept",
    "surfaces": [],
    "body": "Kept.\n",
    "extra": {},
}


def _dead_pid() -> int:
    done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    return int(done.stdout)


def _rig(tmp_path: Path, monkeypatch, *initiatives: str) -> tuple[Path, dict[str, Path]]:
    """A workspace with one rowed task, so the export is not skipped as an empty store, and one unrowed file per initiative."""
    monkeypatch.setattr(store_fill, "fill_workspace", lambda ws: "")
    # The store needs the harness python, absent in CI; the rows come from a stub, as in test_chair_export_tick.py.
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir: [ANCHOR])
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    files = {}
    for initiative in initiatives:
        path = tmp_path / "work" / initiative / "launch" / "task.md"
        path.parent.mkdir(parents=True)
        path.write_text(TICKET)
        files[initiative] = path
    return runs_dir, files


def test_a_live_decompose_keeps_its_unrowed_ticket_file(tmp_path, monkeypatch):
    runs_dir, files = _rig(tmp_path, monkeypatch, "idea")
    (runs_dir / "idea-1.pid").write_text(str(os.getpid()))
    cli._export_board(runs_dir, tmp_path)
    assert files["idea"].exists()


def test_without_a_live_pid_the_unrowed_file_is_removed(tmp_path, monkeypatch):
    runs_dir, files = _rig(tmp_path, monkeypatch, "idea")
    cli._export_board(runs_dir, tmp_path)
    assert not files["idea"].exists()


def test_a_dead_pid_file_does_not_protect_the_unrowed_file(tmp_path, monkeypatch):
    runs_dir, files = _rig(tmp_path, monkeypatch, "idea")
    (runs_dir / "idea-1.pid").write_text(str(_dead_pid()))
    cli._export_board(runs_dir, tmp_path)
    assert not files["idea"].exists()


def test_a_live_decompose_spares_only_its_own_initiative(tmp_path, monkeypatch):
    runs_dir, files = _rig(tmp_path, monkeypatch, "idea", "other")
    (runs_dir / "idea-1.pid").write_text(str(os.getpid()))
    cli._export_board(runs_dir, tmp_path)
    assert (files["idea"].exists(), files["other"].exists()) == (True, False)
