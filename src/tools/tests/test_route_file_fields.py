"""`cox route file --intake --epic|--priority|--target-release`: the fields are checked before anything is written."""

import pytest

from agent_tools import run_store
from agent_tools.cli import main


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A workspace, a profile naming one repo, and an in-memory store holding one initiative of epic `launch-pad`."""
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    repo = tmp_path / "widget"
    (repo / ".git").mkdir(parents=True)
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        f"team: acme\nworkspace_dir: {ws}\nharness_dir: /opt/coxswain-graphs\n"
        "cartridges_dir: /opt/cartridges\nprovider_profile: /opt/providers/acme.yaml\n"
        'repo_map: {"acme/widget": "/src/widget"}\n'
    )
    seeded = {"kind": "initiative", "initiative": "pad-1", "task_id": "pad-1", "title": "Pad", "extra": {"epic": "launch-pad"}}
    rows: dict = {("pad-1", "pad-1"): seeded}

    def upsert(_runs_dir, row):
        rows[(row["initiative"], row["task_id"])] = row
        return True

    monkeypatch.setattr(run_store, "upsert_row", upsert)
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir, initiative=None, kind=None: list(rows.values()))
    monkeypatch.setattr(run_store, "live_lanes", lambda runs_dir, now: [])
    monkeypatch.setattr(run_store, "resolve_initiative", lambda runs_dir, token: token)
    monkeypatch.setattr("agent_tools.cli._sync_filed_items", lambda *_a, **_k: None)
    return {"ws": ws, "profile": str(profile), "repo": str(repo)}


def _file(env, *flags, repo=None, intake=True):
    argv = ["route", "file", "--profile", env["profile"], "--repo", repo or env["repo"], "--title", "Idea"]
    return main([*argv, *(["--intake"] if intake else []), *flags])


def _filed(env):
    return sorted((env["ws"] / "intake").glob("*.md")) if (env["ws"] / "intake").is_dir() else []


def test_an_intake_with_all_fields_is_written_with_them_in_its_frontmatter(env):
    assert _file(env, "--epic", "launch-pad", "--priority", "3", "--target-release", "v2026.11.6") == 0
    [path] = _filed(env)
    head = path.read_text(encoding="utf-8").split("---")[1]
    assert "epic: launch-pad\n" in head
    assert "priority: 3\n" in head
    assert "target_release: 2026.11.6\n" in head


def test_an_unknown_repo_is_refused_and_lists_the_valid_names(env, capsys):
    assert _file(env, repo="nowhere") == 2
    out = capsys.readouterr().out
    assert "is not a git checkout" in out
    assert "valid repos: acme/widget" in out
    assert not _filed(env)


def test_an_unknown_epic_is_refused_and_lists_the_valid_epics(env, capsys):
    assert _file(env, "--epic", "no-such-epic") == 2
    out = capsys.readouterr().out
    assert "epic 'no-such-epic' is not known; valid epics: launch-pad" in out
    assert not _filed(env)


def test_a_malformed_version_is_refused_and_every_refusal_is_printed(env, capsys):
    assert _file(env, "--epic", "no-such-epic", "--priority", "high", "--target-release", "next-week") == 2
    out = capsys.readouterr().out
    assert out.count("routing: ") == 3
    assert "target_release 'next-week' must be a dotted numeric version" in out
    assert "priority 'high' must be a whole number" in out
    assert not _filed(env)


@pytest.mark.parametrize(
    "flag, value, noun",
    [("--epic", "launch-pad", "an epic"), ("--priority", "2", "a priority"), ("--target-release", "1.2", "a target release")],
)
def test_each_field_flag_without_intake_is_refused(env, capsys, flag, value, noun):
    assert _file(env, flag, value, intake=False) == 2
    assert capsys.readouterr().out == f"routing: {flag} needs --intake, since only an intake carries {noun}\n"
    assert not _filed(env)


def test_the_filed_fields_appear_in_the_status_output(env, capsys):
    assert _file(env, "--epic", "launch-pad", "--priority", "3", "--target-release", "2026.11.6") == 0
    capsys.readouterr()
    assert main(["route", "status", "--profile", env["profile"]]) == 0
    out = capsys.readouterr().out
    assert "epic launch-pad" in out
    assert "priority 3" in out
    assert "target 2026.11.6" in out
