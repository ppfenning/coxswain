"""`cox route file --draft|--ready` and `cox route ready|draft`: the stage an intake is filed at and the moves between stages."""

import pytest

from agent_tools import chair_read_intake, run_store
from agent_tools.cli import main


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A workspace, a profile and an in-memory queue store; no tracker and no harness python is called."""
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    repo = tmp_path / "widget"
    (repo / ".git").mkdir(parents=True)
    profile = tmp_path / "profile.yaml"
    base = (
        f"team: acme\nworkspace_dir: {ws}\nharness_dir: /opt/coxswain-graphs\n"
        "cartridges_dir: /opt/cartridges\nprovider_profile: /opt/providers/acme.yaml\n"
    )
    profile.write_text(base)
    rows: dict = {}

    def upsert(_runs_dir, row):
        rows[(row["initiative"], row["task_id"])] = row
        return True

    monkeypatch.setattr(run_store, "upsert_row", upsert)
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir, initiative=None, kind=None: list(rows.values()))
    monkeypatch.setattr(run_store, "live_lanes", lambda runs_dir, now: [])
    monkeypatch.setattr(run_store, "resolve_initiative", lambda runs_dir, token: token)
    monkeypatch.setattr("agent_tools.cli._sync_filed_items", lambda *_a, **_k: None)
    return {"ws": ws, "profile": str(profile), "profile_path": profile, "base": base, "repo": str(repo), "rows": rows}


def _file(env, title, *flags):
    argv = ["route", "file", "--profile", env["profile"], "--repo", env["repo"], "--title", title, "--intake", *flags]
    return main(argv)


def _id(env, title):
    return next(row["task_id"] for row in env["rows"].values() if row["title"] == title)


def _text(env, task_id):
    return env["ws"].joinpath("intake", f"{task_id}.md").read_text(encoding="utf-8")


def test_an_intake_filed_with_no_flag_is_a_draft(env):
    assert _file(env, "Idea") == 0
    assert "stage: draft\n" in _text(env, _id(env, "Idea"))


def test_ready_files_a_ready_intake_and_draft_files_a_draft(env):
    assert _file(env, "Go", "--ready") == 0
    assert _file(env, "Wait", "--draft") == 0
    assert "stage: ready\n" in _text(env, _id(env, "Go"))
    assert "stage: draft\n" in _text(env, _id(env, "Wait"))


def test_draft_and_ready_together_are_refused_in_one_line(env, capsys):
    assert _file(env, "Idea", "--draft", "--ready") == 2
    assert capsys.readouterr().out == "routing: --draft and --ready are exclusive; give one\n"
    assert not env["rows"]


def test_the_stage_flags_without_intake_are_refused(env, capsys):
    argv = ["route", "file", "--profile", env["profile"], "--repo", env["repo"], "--title", "T", "--ready"]
    assert main(argv) == 2
    assert capsys.readouterr().out == "routing: --draft and --ready need --intake, since only an intake has a stage\n"
    assert not env["rows"]


def test_the_profile_intake_stage_is_the_default_and_a_flag_overrides_it(env):
    env["profile_path"].write_text(env["base"] + "intake_stage: ready\n")
    assert _file(env, "Default") == 0
    assert _file(env, "Flagged", "--draft") == 0
    assert "stage: ready\n" in _text(env, _id(env, "Default"))
    assert "stage: draft\n" in _text(env, _id(env, "Flagged"))


def test_ready_and_draft_commands_move_an_intake_and_rewrite_its_file(env):
    assert _file(env, "Idea") == 0
    task_id = _id(env, "Idea")

    assert main(["route", "ready", task_id, "--profile", env["profile"]]) == 0
    assert "stage: ready\n" in _text(env, task_id)
    assert env["rows"][("intake", task_id)]["extra"]["stage"] == "ready"

    assert main(["route", "draft", task_id, "--profile", env["profile"]]) == 0
    assert "stage: draft\n" in _text(env, task_id)
    assert env["rows"][("intake", task_id)]["extra"]["stage"] == "draft"


def test_ready_with_dry_run_writes_nothing(env, capsys):
    assert _file(env, "Idea") == 0
    task_id = _id(env, "Idea")
    before = _text(env, task_id)
    capsys.readouterr()

    assert main(["route", "ready", task_id, "--dry-run", "--profile", env["profile"]]) == 0
    assert _text(env, task_id) == before
    assert env["rows"][("intake", task_id)]["extra"]["stage"] == "draft"


def test_a_draft_is_absent_from_the_chairs_intake_and_a_ready_one_is_present(env):
    assert _file(env, "Draft") == 0
    assert _file(env, "Ready", "--ready") == 0

    rows = run_store.read_queue(None)
    assert {row["task_id"] for row in rows if row["kind"] == "intake"} == {_id(env, "Draft"), _id(env, "Ready")}
    assert [row["task_id"] for row in chair_read_intake.intake_from_rows(rows)] == [_id(env, "Ready")]
