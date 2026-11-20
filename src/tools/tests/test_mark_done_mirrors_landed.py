import datetime
import json
from pathlib import Path

import pytest

from agent_tools import cli, run_store, store_cli

_RECORD = {"task": "seams-task", "state": "approved"}
_PR = "https://example.test/pr/7"
_AT = "2026-09-25T10:00:00+00:00"


def _step(tmp_path: Path, **extra) -> dict:
    path = tmp_path / "runs" / "run-1" / "tasks" / "seams" / "seams-task.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_RECORD), encoding="utf-8")
    return {"kind": "mark_done", "task": "seams-task", "path": str(path), **extra}


def _mirrored(tmp_path: Path) -> dict:
    return _step(tmp_path, run="run-1", phase="seams", pr=_PR, at=_AT)


_NOW = "2026-09-25T09:00:00+00:00"


@pytest.fixture
def harness(tmp_path):
    """The real store under `<tmp_path>/runs`, holding the task's record, so a mirror takes."""
    from harness.store_migrate import open_store
    from harness.store_write import Store

    (tmp_path / "runs").mkdir()
    conn = open_store(store_cli.store_url_for(tmp_path / "runs"), _NOW)
    try:
        Store(conn).record_task_record("run-1", "seams", "seams-task", dict(_RECORD), _NOW)
    finally:
        conn.close()
    return tmp_path


def _landed_in_store(tmp_path: Path):
    return run_store.task_record(tmp_path / "runs", "run-1", "seams", "seams-task")


def _rewritten(path: str) -> str:
    return json.dumps({**_RECORD, "landed": True}, indent=2)


def test_a_present_record_is_marked_landed_with_pr_and_time_and_nothing_extra_is_printed(tmp_path, harness, capsys):
    step = _mirrored(tmp_path)
    ok, detail = cli._execute_land_step(tmp_path, step)
    assert ok is True
    assert _landed_in_store(tmp_path)["landed"] == {"pr": _PR, "at": _AT}
    assert capsys.readouterr().out == ""
    assert Path(step["path"]).read_text(encoding="utf-8") == _rewritten(step["path"])


def test_an_absent_record_says_it_predates_the_mirror_and_carries_on(tmp_path, capsys):
    step = _mirrored(tmp_path)
    ok, detail = cli._execute_land_step(tmp_path, step)
    assert ok is True
    assert "store mirror of seams-task skipped: the record predates the mirror" in capsys.readouterr().out
    assert Path(step["path"]).read_text(encoding="utf-8") == _rewritten(step["path"])


def test_an_unraised_wrapper_failure_warns_and_the_step_still_succeeds(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("agent_tools.store_cli.mark_landed", lambda *args: store_cli.Failed(1, "boom"))
    step = _mirrored(tmp_path)
    ok, detail = cli._execute_land_step(tmp_path, step)
    assert ok is True
    assert "warning: store mirror of seams-task failed (exit 1: boom)" in capsys.readouterr().out
    assert Path(step["path"]).read_text(encoding="utf-8") == _rewritten(step["path"])


def test_an_unexpected_raise_in_the_wrapper_warns_and_the_step_still_succeeds(tmp_path, monkeypatch, capsys):
    def explode(*args):
        raise RuntimeError("wrapper bug")

    monkeypatch.setattr("agent_tools.store_cli.mark_landed", explode)
    ok, detail = cli._execute_land_step(tmp_path, _mirrored(tmp_path))
    assert ok is True
    assert "warning: store mirror of seams-task failed (RuntimeError: wrapper bug)" in capsys.readouterr().out


def test_an_unopenable_store_warns_and_the_step_still_succeeds(tmp_path, store_at, capsys):
    store_at("nosuch://x")
    ok, detail = cli._execute_land_step(tmp_path, _mirrored(tmp_path))
    assert ok is True
    assert "harness not available" in capsys.readouterr().out


def test_a_step_without_the_store_facts_is_not_mirrored(tmp_path, harness, capsys):
    ok, detail = cli._execute_land_step(tmp_path, _step(tmp_path))
    assert ok is True
    assert "landed" not in _landed_in_store(tmp_path)
    assert capsys.readouterr().out == ""


def test_mark_done_facts_reads_run_and_phase_from_the_record_path(tmp_path):
    step = cli._mark_done_facts(_step(tmp_path), _PR, _AT)
    assert (step["run"], step["phase"], step["pr"], step["at"]) == ("run-1", "seams", _PR, _AT)


def test_the_land_walk_returns_success_and_supplies_pr_and_an_iso_time(tmp_path, harness):
    step = _step(tmp_path)
    rc, reached, pr = cli._land_walk(tmp_path, [step], [step], None, None, "merge", False, None, [])
    assert (rc, reached) == (0, ["mark_done"])
    landed = _landed_in_store(tmp_path)["landed"]
    assert landed["pr"] == ""
    datetime.datetime.fromisoformat(landed["at"])
    assert Path(step["path"]).read_text(encoding="utf-8") == _rewritten(step["path"])
