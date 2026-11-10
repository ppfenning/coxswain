"""`cox route file --intake --epic|--priority|--target-release`: bad fields are refused before anything is written."""

import pytest

from agent_tools import run_store
from agent_tools.cli import main

_EPIC_ROW = {"kind": "initiative", "initiative": "alpha", "task_id": "alpha", "extra": {"epic": "launch-wave"}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A workspace, a profile with a `repo_map`, and an in-memory store holding one initiative of epic launch-wave."""
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    repo = tmp_path / "widget"
    (repo / ".git").mkdir(parents=True)
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        f"team: acme\nworkspace_dir: {ws}\nharness_dir: /opt/coxswain-graphs\n"
        "cartridges_dir: /opt/cartridges\nprovider_profile: /opt/providers/acme.yaml\n"
        'repo_map: {"acme/gadget": "/srv/gadget"}\n'
    )
    rows: dict = {("alpha", "alpha"): _EPIC_ROW}

    def upsert(_runs_dir, row):
        rows[(row["initiative"], row["task_id"])] = row
        return True

    monkeypatch.setattr(run_store, "upsert_row", upsert)
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir, initiative=None, kind=None: list(rows.values()))
    monkeypatch.setattr(run_store, "live_lanes", lambda runs_dir, now: [])
    monkeypatch.setattr(run_store, "resolve_initiative", lambda runs_dir, token: token)
    monkeypatch.setattr("agent_tools.cli._sync_filed_items", lambda *_a, **_k: None)
    return {"ws": ws, "profile": str(profile), "repo": str(repo), "rows": rows}


def _file(env, *flags, repo=None, intake=True):
    argv = ["route", "file", "--profile", env["profile"], "--repo", repo or env["repo"], "--title", "Idea", *flags]
    return main([*argv, "--intake"] if intake else argv)


def _intake_files(env):
    return sorted((env["ws"] / "intake").glob("*.md")) if (env["ws"] / "intake").is_dir() else []


def test_an_intake_with_every_field_is_written_with_them(env):
    assert _file(env, "--epic", "launch-wave", "--priority", "3", "--target-release", "v2026.12.0") == 0

    [path] = _intake_files(env)
    text = path.read_text(encoding="utf-8")
    assert "epic: launch-wave\n" in text
    assert "priority: 3\n" in text
    assert "target_release: 2026.12.0\n" in text


def test_an_unknown_repo_is_refused_and_lists_the_valid_names(env, capsys):
    assert _file(env, repo="nowhere") == 2

    out = capsys.readouterr().out
    assert "is not a git checkout" in out
    assert "valid repos: acme/gadget, widget" in out
    assert _intake_files(env) == []


def test_an_unknown_epic_is_refused_and_lists_the_valid_epics(env, capsys):
    assert _file(env, "--epic", "nope") == 2

    assert capsys.readouterr().out == "routing: epic 'nope' is not known; valid epics: launch-wave\n"
    assert _intake_files(env) == []


def test_a_malformed_version_is_refused_and_every_refusal_is_printed(env, capsys):
    assert _file(env, "--target-release", "soon", "--priority", "high") == 2

    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2
    assert out[0].startswith("routing: priority 'high' must be a whole number")
    assert out[1].startswith("routing: target_release 'soon' must be a dotted numeric version")
    assert _intake_files(env) == []
    assert list(env["rows"]) == [("alpha", "alpha")]


@pytest.mark.parametrize(
    ("flag", "value"), [("--epic", "launch-wave"), ("--priority", "2"), ("--target-release", "2026.12.0")]
)
def test_each_field_flag_needs_intake(env, capsys, flag, value):
    assert _file(env, flag, value, intake=False) == 2

    assert capsys.readouterr().out.startswith(f"routing: {flag} needs --intake, since only an intake carries ")
    assert _intake_files(env) == []


def test_the_fields_appear_in_the_status_output(env, capsys):
    assert _file(env, "--epic", "launch-wave", "--priority", "3", "--target-release", "2026.12.0") == 0
    capsys.readouterr()

    assert main(["route", "status", "--profile", env["profile"]]) == 0
    out = capsys.readouterr().out
    assert "epic launch-wave, priority 3, target 2026.12.0" in out
