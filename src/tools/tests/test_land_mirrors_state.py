import json
from pathlib import Path

import pytest

from agent_tools import cli, run_store, store_cli

APPROVED = "---\nid: t1\nstate: approved\n---\nbody\n"
DONE = "---\nid: t1\nstate: done\n---\nbody\n"


def _item(tmp_path: Path, text: str = APPROVED) -> Path:
    item = tmp_path / "work" / "init" / "p1" / "t1.md"
    item.parent.mkdir(parents=True)
    item.write_text(text, encoding="utf-8")
    return item


def _recording(monkeypatch) -> list:
    seen = []
    monkeypatch.setattr(store_cli, "mirror_state", lambda *args: seen.append(args))
    return seen


def test_the_mirror_is_called_with_the_five_arguments_after_the_file_is_written(monkeypatch, tmp_path):
    item = _item(tmp_path)
    seen = []
    monkeypatch.setattr(store_cli, "mirror_state", lambda *args: seen.append((*args, item.read_text(encoding="utf-8"))))
    cli._close_approved_item(str(item), runs_dir=tmp_path / "runs", by="sess-1", merged=True)
    assert seen == [(tmp_path / "runs", "init", "t1", "done", "sess-1", DONE)]


def _seed_row(tmp_path: Path, state: str = "approved") -> None:
    (tmp_path / "runs").mkdir(exist_ok=True)
    assert run_store.upsert_row(tmp_path / "runs", {"kind": "task", "initiative": "init", "task_id": "t1", "phase": "p1",
                                                    "state": state, "needs": [], "surfaces": [], "title": "T", "body": ""})


@pytest.mark.parametrize("case, warns", [("recorded", False), ("no row", True), ("unopenable store", True)])
def test_a_store_outcome_never_undoes_the_move(monkeypatch, tmp_path, capsys, store_at, case, warns):
    item = _item(tmp_path)
    if case == "recorded":
        _seed_row(tmp_path)
    if case == "unopenable store":
        store_at("nosuch://x")
    cli._close_approved_item(str(item), runs_dir=tmp_path / "runs", by="sess-1")
    printed = capsys.readouterr().out
    assert item.read_text(encoding="utf-8") == DONE
    assert ("warning: store did not record init/t1 as done" in printed) is warns
    if case == "recorded":
        assert run_store.work_items(tmp_path / "runs", "init")[0]["state"] == "done"


@pytest.mark.parametrize("text, mirrored", [(DONE, True), ("---\nid: t1\nstate: blocked\n---\nbody\n", False)])
def test_an_item_already_done_is_mirrored_again_and_a_refused_one_is_not(monkeypatch, tmp_path, text, mirrored):
    item = _item(tmp_path, text)
    seen = _recording(monkeypatch)
    cli._close_approved_item(str(item), runs_dir=tmp_path / "runs", by="sess-1")
    assert (item.read_text(encoding="utf-8"), seen) == (text, [(tmp_path / "runs", "init", "t1", "done", "sess-1")] if mirrored else [])


@pytest.mark.parametrize("by, expected", [("sess-1", "sess-1"), (None, "unlabeled")])
def test_the_land_walk_mirrors_its_mark_done_under_the_record_runs_dir(monkeypatch, tmp_path, by, expected):
    record = tmp_path / "runs" / "run-1" / "tasks" / "p1" / "t1.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"task": "t1"}), encoding="utf-8")
    step = {"kind": "mark_done", "task": "t1", "path": str(record), "item": str(_item(tmp_path))}
    seen = _recording(monkeypatch)
    kwargs = {} if by is None else {"by": by}
    rc, reached, _ = cli._land_walk(tmp_path, [step], [step], None, None, "merge", False, None, [], **kwargs)
    assert (rc, reached) == (0, ["mark_done"])
    assert seen == [((tmp_path / "runs").resolve(), "init", "t1", "done", expected)]
