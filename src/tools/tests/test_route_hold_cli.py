"""`cox route hold|release` and the multi-target `cox route remove`: parsing, the file round trip, the chair's view, and the one-id form."""

import copy

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
    profile.write_text(
        f"team: acme\nworkspace_dir: {ws}\nharness_dir: /opt/coxswain-graphs\n"
        "cartridges_dir: /opt/cartridges\nprovider_profile: /opt/providers/acme.yaml\n"
    )
    rows: dict = {}

    def upsert(_runs_dir, row):
        rows[(row["initiative"], row["task_id"])] = row
        return True

    monkeypatch.setattr(run_store, "upsert_row", upsert)
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir, initiative=None, kind=None: list(rows.values()))
    monkeypatch.setattr(run_store, "live_lanes", lambda runs_dir, now: [])
    monkeypatch.setattr(run_store, "resolve_initiative", lambda runs_dir, token: token)
    monkeypatch.setattr("agent_tools.cli._sync_filed_items", lambda *_a, **_k: None)
    return {"ws": ws, "profile": str(profile), "repo": str(repo), "rows": rows}


def _file(env, title):
    assert main(["route", "file", "--profile", env["profile"], "--repo", env["repo"], "--title", title, "--intake", "--ready"]) == 0
    return next(row["task_id"] for row in env["rows"].values() if row["title"] == title)


def _path(env, task_id, *parts):
    return env["ws"].joinpath("intake", *parts, f"{task_id}.md")


def _route(env, verb, *args):
    return main(["route", verb, *args, "--profile", env["profile"]])


def test_hold_and_release_parse_and_hold_needs_a_reason(env, capsys):
    task_id = _file(env, "Idea")
    with pytest.raises(SystemExit) as exc:
        _route(env, "hold", task_id)
    assert exc.value.code == 2
    assert _route(env, "hold", task_id, "--reason", "waiting", "--by", "pat", "--dry-run") == 0
    assert _route(env, "release", task_id, "--dry-run") == 0
    assert "would hold intake" in capsys.readouterr().out


def test_hold_then_release_round_trips_the_intake_file(env):
    task_id = _file(env, "Idea")
    original = _path(env, task_id).read_bytes()

    assert _route(env, "hold", task_id, "--reason", "waiting on design", "--by", "pat") == 0
    held = _path(env, task_id).read_text(encoding="utf-8")
    assert "stage: draft\n" in held and "held:" not in held
    assert "waiting on design" in held

    assert _route(env, "release", task_id) == 0
    released = _path(env, task_id).read_bytes()
    assert b"stage: ready\n" in released and b"hold_reason" not in released
    assert released == original


def test_a_held_intake_stays_in_the_queue_rows_but_not_in_the_chairs_runnable_intake(env):
    kept, parked = _file(env, "Kept"), _file(env, "Parked")
    assert _route(env, "hold", parked, "--reason", "waiting", "--by", "pat") == 0

    rows = run_store.read_queue(None)
    assert {row["task_id"] for row in rows if row["kind"] == "intake"} == {kept, parked}
    assert [row["task_id"] for row in chair_read_intake.intake_from_rows(rows)] == [kept]


def test_remove_with_two_ids_moves_both_and_records_both_done(env, capsys):
    first, second = _file(env, "Fix alpha"), _file(env, "Fix beta")
    capsys.readouterr()

    assert _route(env, "remove", first, second, "--reason", "dup", "--by", "pat") == 0

    assert "removed 2 of 2" in capsys.readouterr().out
    for task_id in (first, second):
        assert not _path(env, task_id).exists()
        assert _path(env, task_id, "done").is_file()
        assert env["rows"][("intake", task_id)]["state"] == "done"


def test_remove_with_match_and_dry_run_writes_nothing(env, capsys):
    ids = [_file(env, "Fix alpha"), _file(env, "Fix beta")]
    _file(env, "Other")
    before_rows, before_files = copy.deepcopy(env["rows"]), {i: _path(env, i).read_bytes() for i in ids}
    capsys.readouterr()

    assert _route(env, "remove", "--match", "fix-*", "--reason", "dup", "--by", "pat", "--dry-run") == 0

    assert "would remove 2 of 2" in capsys.readouterr().out
    assert env["rows"] == before_rows
    assert {i: _path(env, i).read_bytes() for i in ids} == before_files
    assert not (env["ws"] / "intake" / "done").exists()


def test_remove_with_neither_an_id_nor_match_is_refused_in_one_line(env, capsys):
    _file(env, "Idea")
    capsys.readouterr()

    assert _route(env, "remove", "--reason", "dup") == 2
    assert capsys.readouterr().out == "remove: give an id or --match\n"


def test_remove_with_one_id_prints_and_returns_what_it_did_before(env, capsys):
    task_id = _file(env, "Idea")
    capsys.readouterr()

    assert _route(env, "remove", task_id, "--reason", "dup", "--by", "pat") == 0

    out = capsys.readouterr().out
    assert out.startswith(f"remove: intake {task_id}: moved to ")
    assert "removed by pat on " in out
    assert " of " not in out
    assert env["rows"][("intake", task_id)]["state"] == "done"
