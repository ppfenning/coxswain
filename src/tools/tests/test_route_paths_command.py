from pathlib import Path

import pytest

from agent_tools import cli, store_fill
from agent_tools.cli import main


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


def _workspace(tmp_path: Path, repo: str) -> Path:
    ws = tmp_path / "ws"
    (ws / "work" / "demo" / "1-build").mkdir(parents=True)
    (ws / "work" / "demo" / "initiative.md").write_text(f"---\ntitle: Demo\nrepo: {repo}\n---\n\nAbout demo\n")
    (ws / "work" / "demo" / "1-build" / "task.md").write_text(f"---\nstate: ready\ntitle: Do it\nrepo: {repo}\n---\n\nDo it\n")
    return ws


def _change_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if " -> " in line]


@pytest.mark.usefixtures("board_store")
def test_dry_run_prints_two_changes_and_writes_nothing(tmp_path: Path, home: Path, capsys) -> None:
    repo = str(home / "repos" / "demo")
    ws = _workspace(tmp_path, repo)
    store_fill.fill_workspace(ws)  # the store is the board: the files' rows are what the command reads
    before = {p: p.read_bytes() for p in ws.rglob("*.md")}
    assert main(["route", "paths", "--workspace", str(ws)]) == 0
    out = capsys.readouterr().out
    assert len(_change_lines(out)) == 2
    assert f"{repo} -> ~/repos/demo" in out
    assert out.splitlines()[-1] == "2 change(s)"
    assert {p: p.read_bytes() for p in ws.rglob("*.md")} == before


@pytest.mark.usefixtures("board_store")
def test_apply_rewrites_both_files_and_a_second_run_finds_nothing(tmp_path: Path, home: Path, capsys) -> None:
    ws = _workspace(tmp_path, str(home / "repos" / "demo"))
    store_fill.fill_workspace(ws)  # the store is the board: the files' rows are what the command reads
    assert main(["route", "paths", "--workspace", str(ws), "--apply"]) == 0
    capsys.readouterr()
    for rel in ("work/demo/initiative.md", "work/demo/1-build/task.md"):
        assert "repo: ~/repos/demo\n" in (ws / rel).read_text()
    assert main(["route", "paths", "--workspace", str(ws)]) == 0
    out = capsys.readouterr().out
    assert _change_lines(out) == []
    assert out.splitlines()[-1] == "0 change(s)"


def _row(task: str, repo: str) -> dict:
    return {"kind": "task", "initiative": "demo", "task_id": task, "phase": "1-build", "state": "ready",
            "needs": [], "title": task, "surfaces": [], "body": "", "extra": {"repo": repo}}


def _store_mode(monkeypatch, rows: list[dict], upsert) -> list[tuple]:
    exports: list[tuple] = []
    monkeypatch.setattr(cli.work_state, "work_state_mode", lambda profile: "store")
    monkeypatch.setattr(cli.run_store, "read_queue", lambda runs_dir, *a, **k: rows)
    monkeypatch.setattr(cli.run_store, "upsert_row", upsert)
    monkeypatch.setattr(cli, "_export_board", lambda runs_dir, ws: exports.append((runs_dir, ws)) or [])
    return exports


def test_store_apply_upserts_only_changed_rows_and_exports_once(tmp_path: Path, home: Path, monkeypatch) -> None:
    ws = _workspace(tmp_path, "~/repos/demo")
    rows = [_row("a", str(home / "repos" / "a")), _row("b", "~/repos/b"), _row("c", str(home / "repos" / "c"))]
    upserted: list[dict] = []
    exports = _store_mode(monkeypatch, rows, lambda runs_dir, row: upserted.append(row) or True)
    assert main(["route", "paths", "--workspace", str(ws), "--apply"]) == 0
    assert upserted == [
        {"initiative": "demo", "task_id": "a", "extra": {"repo": "~/repos/a"}, "surfaces": []},
        {"initiative": "demo", "task_id": "c", "extra": {"repo": "~/repos/c"}, "surfaces": []},
    ]
    assert exports == [(ws / "runs", ws)]


def test_store_apply_never_sends_state_needs_or_claim_columns(tmp_path: Path, home: Path, monkeypatch) -> None:
    ws = _workspace(tmp_path, "~/repos/demo")
    claimed = {**_row("a", str(home / "repos" / "a")), "state": "doing", "needs": ["x"], "holder": "h", "epoch": 3, "expires_at": "t"}
    upserted: list[dict] = []
    _store_mode(monkeypatch, [claimed], lambda runs_dir, row: upserted.append(row) or True)
    assert main(["route", "paths", "--workspace", str(ws), "--apply"]) == 0
    assert set(upserted[0]) == {"initiative", "task_id", "extra", "surfaces"}


def test_an_empty_read_from_an_unreadable_store_is_an_error_not_zero_changes(
    tmp_path: Path, home: Path, monkeypatch, capsys
) -> None:
    ws = _workspace(tmp_path, "~/repos/demo")
    _store_mode(monkeypatch, [], lambda runs_dir, row: True)
    monkeypatch.setattr("agent_tools.store_fill._read_store", lambda runs_dir: None)
    assert main(["route", "paths", "--workspace", str(ws)]) == 2
    assert "cannot be read" in capsys.readouterr().out


def test_a_failed_export_after_the_upserts_is_reported_and_exits_non_zero(
    tmp_path: Path, home: Path, monkeypatch, capsys
) -> None:
    ws = _workspace(tmp_path, "~/repos/demo")
    _store_mode(monkeypatch, [_row("a", str(home / "repos" / "a"))], lambda runs_dir, row: True)

    def boom(runs_dir: Path, workspace: Path) -> list[str]:
        raise RuntimeError("store unreadable")

    monkeypatch.setattr(cli, "_export_board", boom)
    assert main(["route", "paths", "--workspace", str(ws), "--apply"]) == 1
    assert "file export failed: store unreadable" in capsys.readouterr().out


def test_store_dry_run_writes_nothing(tmp_path: Path, home: Path, monkeypatch, capsys) -> None:
    ws = _workspace(tmp_path, "~/repos/demo")
    upserted: list[dict] = []
    exports = _store_mode(monkeypatch, [_row("a", str(home / "repos" / "a"))], lambda runs_dir, row: upserted.append(row) or True)
    assert main(["route", "paths", "--workspace", str(ws)]) == 0
    assert (upserted, exports) == ([], [])
    assert len(_change_lines(capsys.readouterr().out)) == 1


def test_a_refused_row_is_reported_the_rest_are_tried_and_the_exit_is_non_zero(
    tmp_path: Path, home: Path, monkeypatch, capsys
) -> None:
    ws = _workspace(tmp_path, "~/repos/demo")
    rows = [_row("a", str(home / "repos" / "a")), _row("b", str(home / "repos" / "b"))]
    tried: list[str] = []
    exports = _store_mode(monkeypatch, rows, lambda runs_dir, row: tried.append(row["task_id"]) or row["task_id"] != "a")
    assert main(["route", "paths", "--workspace", str(ws), "--apply"]) == 1
    assert tried == ["a", "b"]
    assert "store refused demo/a" in capsys.readouterr().out
    assert len(exports) == 1


@pytest.mark.usefixtures("board_store")
def test_move_dry_run_lists_the_swaps_and_writes_nothing(tmp_path: Path, home: Path, capsys) -> None:
    ws = _workspace(tmp_path, "~/repos/old/demo")
    store_fill.fill_workspace(ws)  # the store is the board: the files' rows are what the command reads
    before = {p: p.read_bytes() for p in ws.rglob("*.md")}
    assert main(["route", "paths", "--workspace", str(ws), "--move", str(home / "repos" / "old"), "~/repos/new"]) == 0
    out = capsys.readouterr().out
    assert len(_change_lines(out)) == 2
    assert "~/repos/old/demo -> ~/repos/new/demo" in out
    assert "still under" not in out
    assert {p: p.read_bytes() for p in ws.rglob("*.md")} == before


@pytest.mark.usefixtures("board_store")
def test_move_apply_rewrites_the_files_and_reports_no_residue(tmp_path: Path, home: Path, capsys) -> None:
    ws = _workspace(tmp_path, "~/repos/old/demo")
    store_fill.fill_workspace(ws)  # the store is the board: the files' rows are what the command reads
    argv = ["route", "paths", "--workspace", str(ws), "--apply", "--move", "~/repos/old", "~/repos/new"]
    assert main(argv) == 0
    out = capsys.readouterr().out
    for rel in ("work/demo/initiative.md", "work/demo/1-build/task.md"):
        assert "repo: ~/repos/new/demo\n" in (ws / rel).read_text()
    assert out.splitlines()[-1] == "route paths: 0 value(s) still under ~/repos/old"


def test_move_apply_in_store_mode_sends_only_moved_columns(tmp_path: Path, home: Path, monkeypatch) -> None:
    ws = _workspace(tmp_path, "~/repos/demo")
    rows = [_row("a", "~/repos/old/a")]
    upserted: list[dict] = []

    def upsert(runs_dir: Path, row: dict) -> bool:
        upserted.append(row)
        rows[:] = [{**r, **row} for r in rows]  # the store now holds the moved values
        return True

    exports = _store_mode(monkeypatch, rows, upsert)
    assert main(["route", "paths", "--workspace", str(ws), "--apply", "--move", "~/repos/old", "~/repos/new"]) == 0
    assert upserted == [{"initiative": "demo", "task_id": "a", "extra": {"repo": "~/repos/new/a"}, "surfaces": []}]
    assert len(exports) == 1


def test_a_value_left_under_old_after_apply_prints_the_count_and_exits_one(
    tmp_path: Path, home: Path, monkeypatch, capsys
) -> None:
    ws = _workspace(tmp_path, "~/repos/demo")
    rows = [_row("a", "~/repos/old/a"), _row("b", "~/repos/old/b")]
    _store_mode(monkeypatch, rows, lambda runs_dir, row: True)  # read_queue keeps returning the stale rows
    assert main(["route", "paths", "--workspace", str(ws), "--apply", "--move", "~/repos/old", "~/repos/new"]) == 1
    assert capsys.readouterr().out.splitlines()[-1] == "route paths: 2 value(s) still under ~/repos/old"


@pytest.mark.usefixtures("board_store")
def test_apply_without_move_prints_no_residue_line(tmp_path: Path, home: Path, capsys) -> None:
    ws = _workspace(tmp_path, str(home / "repos" / "demo"))
    store_fill.fill_workspace(ws)  # the store is the board: the files' rows are what the command reads
    assert main(["route", "paths", "--workspace", str(ws), "--apply"]) == 0
    assert "still under" not in capsys.readouterr().out


def test_a_workspace_without_work_or_intake_is_refused(tmp_path: Path, home: Path, capsys) -> None:
    assert main(["route", "paths", "--workspace", str(tmp_path)]) == 2
    assert "no work/ or intake/" in capsys.readouterr().out


def _two_initiatives(tmp_path: Path, repo: str) -> Path:
    ws = tmp_path / "ws"
    for name, state in (("open", "ready"), ("shipped", "done")):
        (ws / "work" / name / "1-build").mkdir(parents=True)
        # A real initiative.md carries no state: its tasks decide whether the initiative is done.
        (ws / "work" / name / "initiative.md").write_text(f"---\ntitle: {name}\nrepo: {repo}/{name}\n---\n\nAbout\n")
        (ws / "work" / name / "1-build" / "task.md").write_text(f"---\nstate: {state}\ntitle: t\nrepo: {repo}/{name}\n---\n\nDo\n")
    return ws


def _snapshot(ws: Path, name: str) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in (ws / "work" / name).rglob("*.md")}


@pytest.mark.usefixtures("board_store")
def test_move_dry_run_lists_only_the_open_initiative_and_writes_nothing(tmp_path: Path, home: Path, capsys) -> None:
    ws = _two_initiatives(tmp_path, "~/repos/old")
    store_fill.fill_workspace(ws)  # the store is the board: the files' rows are what the command reads
    before = {p: p.read_bytes() for p in ws.rglob("*.md")}
    assert main(["route", "paths", "--workspace", str(ws), "--move", "~/repos/old", "~/repos/new"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("route paths: only open records are moved")
    assert len(_change_lines(out)) == 2
    assert all("/open" in line for line in _change_lines(out))
    assert "shipped" not in out
    assert {p: p.read_bytes() for p in ws.rglob("*.md")} == before


@pytest.mark.usefixtures("board_store")
def test_move_apply_rewrites_the_open_initiative_and_leaves_the_done_one_untouched(
    tmp_path: Path, home: Path, capsys
) -> None:
    ws = _two_initiatives(tmp_path, "~/repos/old")
    store_fill.fill_workspace(ws)  # the store is the board: the files' rows are what the command reads
    done_before = _snapshot(ws, "shipped")
    assert main(["route", "paths", "--workspace", str(ws), "--apply", "--move", "~/repos/old", "~/repos/new"]) == 0
    out = capsys.readouterr().out
    for rel in ("work/open/initiative.md", "work/open/1-build/task.md"):
        assert "repo: ~/repos/new/open\n" in (ws / rel).read_text()
    assert _snapshot(ws, "shipped") == done_before
    assert out.splitlines()[-1] == "route paths: 0 value(s) still under ~/repos/old"


@pytest.mark.usefixtures("board_store")
def test_a_run_without_move_still_lists_both_initiatives(tmp_path: Path, home: Path, capsys) -> None:
    ws = _two_initiatives(tmp_path, str(home / "repos"))
    store_fill.fill_workspace(ws)  # the store is the board: the files' rows are what the command reads
    assert main(["route", "paths", "--workspace", str(ws)]) == 0
    out = capsys.readouterr().out
    assert len(_change_lines(out)) == 4
    assert "only open records" not in out
    assert "/open" in out and "/shipped" in out


def test_move_apply_in_store_mode_skips_done_rows_and_does_not_count_them(
    tmp_path: Path, home: Path, monkeypatch, capsys
) -> None:
    ws = _workspace(tmp_path, "~/repos/demo")
    rows = [_row("a", "~/repos/old/a"), dict(_row("b", "~/repos/old/b"), state="done")]
    upserted: list[dict] = []

    def upsert(runs_dir: Path, row: dict) -> bool:
        upserted.append(row)
        rows[:] = [dict(r, **row) if r["task_id"] == row["task_id"] else r for r in rows]
        return True

    _store_mode(monkeypatch, rows, upsert)
    assert main(["route", "paths", "--workspace", str(ws), "--apply", "--move", "~/repos/old", "~/repos/new"]) == 0
    assert [u["task_id"] for u in upserted] == ["a"]
    assert capsys.readouterr().out.splitlines()[-1] == "route paths: 0 value(s) still under ~/repos/old"


def test_move_apply_in_store_mode_leaves_a_done_initiatives_row_alone(
    tmp_path: Path, home: Path, monkeypatch, capsys
) -> None:
    ws = _workspace(tmp_path, "~/repos/demo")

    def row(kind: str, initiative: str, task_id: str, state: str) -> dict:
        return dict(_row(task_id, f"~/repos/old/{initiative}"), kind=kind, initiative=initiative, state=state)

    rows = [
        row("initiative", "live", "live", "todo"),
        row("task", "live", "a", "ready"),
        row("initiative", "shipped", "shipped", "todo"),  # the store gives an initiative row the default state
        row("task", "shipped", "b", "done"),
    ]
    upserted: list[dict] = []

    def upsert(runs_dir: Path, new: dict) -> bool:
        upserted.append(new)
        rows[:] = [dict(r, **new) if (r["initiative"], r["task_id"]) == (new["initiative"], new["task_id"]) else r for r in rows]
        return True

    _store_mode(monkeypatch, rows, upsert)
    assert main(["route", "paths", "--workspace", str(ws), "--apply", "--move", "~/repos/old", "~/repos/new"]) == 0
    out = capsys.readouterr().out
    assert [(u["initiative"], u["task_id"]) for u in upserted] == [("live", "live"), ("live", "a")]
    assert "shipped" not in out
    assert out.splitlines()[-1] == "route paths: 0 value(s) still under ~/repos/old"


def test_open_files_keeps_an_initiative_unless_it_has_tasks_and_all_are_terminal() -> None:
    def task(state: str) -> str:
        return f"---\nstate: {state}\n---\n\nbody\n"

    head = "---\ntitle: x\n---\n\nAbout\n"
    files = {
        "work/all-done/initiative.md": head,
        "work/all-done/1-a/t.md": task("done"),
        "work/all-done/1-a/u.md": task("dropped"),
        "work/mixed/initiative.md": head,
        "work/mixed/1-a/t.md": task("done"),
        "work/mixed/1-a/u.md": task("ready"),
        "work/no-tasks/initiative.md": head,
        "work/stated/initiative.md": "---\ntitle: x\nstate: done\n---\n\nAbout\n",
        "intake/note.md": head,
        "work/noted/initiative.md": head,
        "work/noted/1-a/t.md": task("done"),
        "work/noted/1-a/NOTES.md": "plain notes, no frontmatter\n",
    }
    assert sorted(cli._open_files(files)) == [
        "intake/note.md",
        "work/mixed/1-a/u.md",
        "work/mixed/initiative.md",
        "work/no-tasks/initiative.md",
        "work/noted/1-a/NOTES.md",
    ]
