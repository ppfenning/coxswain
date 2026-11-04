import json
import sys

import pytest
from test_route_cli import _unmeasured_window, _wait_for, _write_harness, _write_launch_profile

from agent_tools import cli, route, run_store, usage_window
from agent_tools.cli import main

INTAKE = {
    "kind": "intake", "initiative": "intake", "task_id": "fix-thing", "phase": "", "state": "queued",
    "needs": [], "title": "Fix thing", "surfaces": [], "body": "Body\n", "extra": {},
}
REPO = "git@example.com:acme/widget.git"
RESULT = {
    "phases": [{"id": "p1", "goal": "g1"}],
    "tasks": [{"id": "t1", "phase": "p1", "title": "T1", "body": "do one", "needs": [], "surfaces": ["a.py"]}],
}


@pytest.fixture(autouse=True)
def _no_usage_gather(monkeypatch):
    monkeypatch.setattr(usage_window, "gather", _unmeasured_window)


def _launch(tmp_path, monkeypatch, mode, intake_rows, edge, dry_run=False, extra=()):
    """Run `route launch decompose` over a fake store; return (exit code, upserted rows, harness dir, workspace)."""
    upserts = []
    monkeypatch.setattr("agent_tools.work_state.work_state_mode", lambda _provider: mode)
    monkeypatch.setattr(
        run_store, "read_queue",
        lambda runs_dir, initiative=None, kind=None: [r for r in intake_rows if kind in (None, r["kind"])] if kind else [],
    )
    monkeypatch.setattr(run_store, "upsert_row_detail", lambda runs_dir, row: upserts.append(row) or "")
    monkeypatch.setattr(cli, "_run_decompose_for_result", edge)
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True, exist_ok=True)
    (ws / "intake").mkdir(parents=True)
    idea = ws / "intake" / "fix-thing.md"
    idea.write_text(f"---\nid: fix-thing\ntitle: Fix thing\nrepo: {REPO}\n---\n\nBody\n")
    profile = _write_launch_profile(tmp_path, harness_dir, ws)
    argv = ["route", "launch", "decompose", "--profile", str(profile), "--idea", str(idea), "--initiative-id", "fix-thing"]
    argv += list(extra)
    code = main([*argv, "--dry-run"] if dry_run else argv)
    return code, upserts, harness_dir, ws


def test_a_free_run_id_names_the_decompose_run(tmp_path, monkeypatch):
    code, _, _, ws = _launch(tmp_path, monkeypatch, "files", [INTAKE], _must_not_run, extra=("--run-id", "fix-thing-7"))

    assert code == 0
    assert _wait_for(ws / "runs" / "fix-thing-7.pid")
    assert not (ws / "runs" / "fix-thing-1.pid").exists()


def test_a_taken_run_id_is_refused_and_starts_nothing(tmp_path, monkeypatch, capsys):
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "runs" / "fix-thing-7.log").write_text("earlier\n")

    code, _, _, _ = _launch(tmp_path, monkeypatch, "files", [INTAKE], _must_not_run, extra=("--run-id", "fix-thing-7"))

    assert code == 2
    assert not (ws / "runs" / "fix-thing-7.pid").exists()
    assert "fix-thing-7" in capsys.readouterr().out


def _must_not_run(*_args, **_kwargs):
    raise AssertionError("called")


def test_helper_is_true_only_for_a_real_store_run_here_with_an_intake_row():
    assert cli._decompose_via_rows("store", False, None, INTAKE) is True
    assert cli._decompose_via_rows("files", False, None, INTAKE) is False
    assert cli._decompose_via_rows("store", True, None, INTAKE) is False
    assert cli._decompose_via_rows("store", False, object(), INTAKE) is False
    assert cli._decompose_via_rows("store", False, None, None) is False


def test_store_mode_writes_the_plan_as_rows_and_no_ticket_file(tmp_path, monkeypatch, capsys):
    seen = []

    def edge(argv, env, log_path, result_path, pid_path):
        seen.append((argv, result_path))
        return 0, RESULT

    code, upserts, harness_dir, ws = _launch(tmp_path, monkeypatch, "store", [INTAKE], edge)

    assert code == 0
    assert [(r["kind"], r["task_id"]) for r in upserts] == [
        ("initiative", "fix-thing"), ("task", "t1"), ("intake", "fix-thing"),
    ]
    assert upserts[-1]["extra"]["initiative"] == "fix-thing"
    argv, result_path = seen[0]
    assert argv[argv.index("--result-out") + 1] == str(result_path) == str(ws / "runs" / "fix-thing-1.result.json")
    assert "routing: wrote 3 rows for fix-thing" in capsys.readouterr().out
    assert not (ws / "work").exists()
    assert not (harness_dir / "recorded_argv.json").exists()


def test_store_mode_with_an_empty_plan_writes_no_rows_and_reports_why(tmp_path, monkeypatch, capsys):
    code, upserts, _harness_dir, ws = _launch(
        tmp_path, monkeypatch, "store", [INTAKE], lambda *_a: (0, {"phases": [], "tasks": []}),
    )

    assert (code, upserts) == (1, [])
    assert "routing: decompose produced no tasks" in capsys.readouterr().out
    assert not (ws / "work").exists()


def test_store_mode_with_no_readable_result_returns_1_and_writes_no_rows(tmp_path, monkeypatch, capsys):
    code, upserts, _harness_dir, _ws = _launch(tmp_path, monkeypatch, "store", [INTAKE], lambda *_a: (0, None))

    assert (code, upserts) == (1, [])
    assert "routing: decompose wrote no readable result at" in capsys.readouterr().out


def test_store_mode_with_a_failed_run_and_a_valid_result_writes_no_rows(tmp_path, monkeypatch, capsys):
    code, upserts, _harness_dir, _ws = _launch(tmp_path, monkeypatch, "store", [INTAKE], lambda *_a: (3, RESULT))

    assert (code, upserts) == (1, [])
    assert "routing: decompose exited 3; no rows written" in capsys.readouterr().out


def test_edge_returns_the_result_json_and_none_for_missing_invalid_or_non_object(tmp_path):
    result = tmp_path / "result.json"
    script = "import sys, pathlib; c = sys.argv[2]; c and pathlib.Path(sys.argv[1]).write_text(c); sys.exit(int(sys.argv[3]))"
    pid = tmp_path / "x.pid"

    def run(content, code=0):
        return cli._run_decompose_for_result(
            [sys.executable, "-c", script, str(result), content, str(code)], {}, tmp_path / "x.log", result, pid,
        )

    result.write_text(json.dumps({"stale": True}))
    assert run("") == (0, None)  # a stale file from an earlier run is not read back
    assert run("{not json") == (0, None)
    assert run("[1]") == (0, None)
    assert run(json.dumps(RESULT)) == (0, RESULT)
    assert run(json.dumps(RESULT), 3) == (3, RESULT)
    assert not pid.exists()


def test_files_mode_launches_detached_and_calls_neither_edge_nor_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(route, "decompose_into_rows", _must_not_run)

    code, upserts, harness_dir, ws = _launch(tmp_path, monkeypatch, "files", [INTAKE], _must_not_run)

    assert code == 0 and upserts == []
    assert _wait_for(ws / "runs" / "fix-thing-1.pid")
    assert _wait_for(harness_dir / "recorded_argv.json")
    assert "--result-out" not in json.loads((harness_dir / "recorded_argv.json").read_text())
    assert (ws / "work" / "fix-thing" / "initiative.md").exists()


def test_store_mode_with_no_intake_row_falls_through_to_the_detached_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(route, "decompose_into_rows", _must_not_run)

    code, upserts, harness_dir, ws = _launch(tmp_path, monkeypatch, "store", [], _must_not_run)

    assert code == 0 and upserts == []
    assert _wait_for(ws / "runs" / "fix-thing-1.pid")
    assert _wait_for(harness_dir / "recorded_argv.json")


def test_store_mode_dry_run_runs_nothing_and_writes_no_rows(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(route, "decompose_into_rows", _must_not_run)

    code, upserts, _harness_dir, ws = _launch(tmp_path, monkeypatch, "store", [INTAKE], _must_not_run, dry_run=True)

    assert (code, upserts) == (0, [])
    assert "dry-run: " in capsys.readouterr().out
    assert not (ws / "runs" / "fix-thing-1.pid").exists()
