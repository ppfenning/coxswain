import subprocess

import pytest
from test_cli_route_launch_decompose_rows import INTAKE, RESULT, _must_not_run
from test_route_cli import _unmeasured_window, _wait_for, _write_harness, _write_launch_profile

from agent_tools import cli, route, run_store, usage_window
from agent_tools.cli import main

REPO = "~/repos/widget"


@pytest.fixture(autouse=True)
def _no_usage_gather(monkeypatch):
    monkeypatch.setattr(usage_window, "gather", _unmeasured_window)


def _launch(tmp_path, monkeypatch, mode, idea_text, edge):
    """Run `route launch decompose` over a fake store; return (exit code, upserted rows, harness dir, workspace)."""
    upserts = []
    monkeypatch.setattr("agent_tools.work_state.work_state_mode", lambda _provider: mode)
    monkeypatch.setattr(
        run_store, "read_queue",
        lambda runs_dir, initiative=None, kind=None: [INTAKE] if kind == "intake" else [],
    )
    monkeypatch.setattr(run_store, "upsert_row_detail", lambda runs_dir, row: upserts.append(row) or "")
    monkeypatch.setattr(cli, "_run_decompose_for_result", edge)
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "intake").mkdir(parents=True)
    idea = ws / "intake" / "fix-thing.md"
    idea.write_text(idea_text)
    profile = _write_launch_profile(tmp_path, harness_dir, ws)
    argv = ["route", "launch", "decompose", "--profile", str(profile), "--idea", str(idea), "--initiative-id", "fix-thing"]
    return main(argv), upserts, harness_dir, ws


@pytest.mark.parametrize("mode", ["files", "store"])
def test_an_intake_with_no_repo_is_refused_and_starts_nothing(tmp_path, monkeypatch, capsys, mode):
    monkeypatch.setattr(subprocess, "Popen", _must_not_run)

    code, upserts, _harness_dir, ws = _launch(
        tmp_path, monkeypatch, mode, "---\nid: fix-thing\ntitle: Fix thing\n---\n\nBody\n", _must_not_run,
    )

    assert (code, upserts) == (2, [])
    assert capsys.readouterr().out == "routing: intake fix-thing has no repo: field\n"
    assert not (ws / "work").exists()
    assert list((ws / "runs").iterdir()) == []


def test_a_store_mode_launch_writes_the_intake_repo_onto_the_initiative_row(tmp_path, monkeypatch):
    code, upserts, _harness_dir, _ws = _launch(
        tmp_path, monkeypatch, "store", f"---\nid: fix-thing\ntitle: Fix thing\nrepo: {REPO}\n---\n\nBody\n",
        lambda *_a: (0, RESULT),
    )

    assert code == 0
    initiative = next(r for r in upserts if r["kind"] == "initiative")
    assert initiative["extra"]["repo"] == REPO


def test_a_files_mode_launch_writes_the_intake_repo_into_initiative_md(tmp_path, monkeypatch):
    code, upserts, harness_dir, ws = _launch(
        tmp_path, monkeypatch, "files", f"---\nid: fix-thing\ntitle: Fix thing\nrepo: {REPO}\n---\n\nBody\n", _must_not_run,
    )

    assert code == 0 and upserts == []
    assert _wait_for(harness_dir / "recorded_argv.json")
    fields, _body = route.parse_frontmatter((ws / "work" / "fix-thing" / "initiative.md").read_text())
    assert fields["repo"] == REPO
