"""`cox route file|edit|status|priority` expose an intake's `after`, its held state and its priority."""

import json

import pytest

from agent_tools import queue_export, route, run_store
from agent_tools.cli import _after_ids, main


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


def _file(env, title="Fix the thing", *extra):
    return main(["route", "file", "--profile", env["profile"], "--repo", env["repo"], "--title", title, *extra])


def _edit(env, task_id, *extra):
    return main(["route", "edit", task_id, "--profile", env["profile"], *extra])


def _intake(env, title):
    return next(row for row in env["rows"].values() if row["title"] == title)


def _fields(env, row):
    return route.parse_frontmatter((env["ws"] / queue_export.row_path(row)).read_text(encoding="utf-8"))[0]


@pytest.mark.parametrize(
    ("given", "ids"),
    [(None, None), ([], []), ([""], []), (["a"], ["a"]), (["a,b"], ["a", "b"]), (["a", " b , c"], ["a", "b", "c"])],
)
def test_after_ids_reads_repeated_and_comma_separated_values(given, ids):
    assert _after_ids(given) == ids


def test_filing_an_intake_with_after_carries_the_list_in_front_matter(env):
    assert _file(env, "Fix the thing", "--intake", "--after", "alpha,beta", "--after", "gamma") == 0
    row = _intake(env, "Fix the thing")
    assert _fields(env, row)["after"] == ["alpha", "beta", "gamma"]
    assert row["extra"]["after"] == ["alpha", "beta", "gamma"]


def test_after_without_intake_is_refused_and_writes_nothing(env, capsys):
    assert _file(env, "Fix the thing", "--after", "alpha") == 2
    assert "--after needs --intake" in capsys.readouterr().out
    assert env["rows"] == {}
    assert not (env["ws"] / "intake").exists()


def test_editing_after_sets_then_an_empty_value_clears_it(env):
    assert _file(env, "Fix the thing", "--intake") == 0
    task_id = _intake(env, "Fix the thing")["task_id"]

    assert _edit(env, task_id, "--after", "alpha,beta") == 0
    assert _fields(env, _intake(env, "Fix the thing"))["after"] == ["alpha", "beta"]

    assert _edit(env, task_id, "--after", "") == 0
    row = _intake(env, "Fix the thing")
    assert "after" not in _fields(env, row)
    assert "after" not in row["extra"]


def test_editing_with_after_absent_leaves_the_list_alone(env):
    assert _file(env, "Fix the thing", "--intake", "--after", "alpha") == 0
    task_id = _intake(env, "Fix the thing")["task_id"]
    assert _edit(env, task_id, "--title", "Fix the other thing") == 0
    assert _fields(env, _intake(env, "Fix the other thing"))["after"] == ["alpha"]


def test_status_shows_a_waiting_line_and_a_held_section(env, capsys):
    assert _file(env, "Waits", "--intake", "--ready", "--after", "nothing-landed") == 0
    assert _file(env, "Parked", "--intake", "--ready") == 0
    parked = _intake(env, "Parked")
    parked["extra"] = {**parked["extra"], "held": True, "hold_reason": "waiting on a decision"}
    capsys.readouterr()

    assert main(["route", "status", "--profile", env["profile"]]) == 0
    out = capsys.readouterr().out

    assert "intake queued: 2 — parked [stage ready, repo widget], waits (waiting on nothing-landed) [stage ready, repo widget]" in out
    assert f"held:\n  {parked['task_id']}: waiting on a decision" in out


def test_status_json_gains_waiting_and_held(env, capsys):
    assert _file(env, "Waits", "--intake", "--ready", "--after", "nothing-landed") == 0
    assert _file(env, "Parked", "--intake", "--ready") == 0
    parked = _intake(env, "Parked")
    parked["extra"] = {**parked["extra"], "held": True}
    capsys.readouterr()

    assert main(["route", "status", "--profile", env["profile"], "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)

    assert doc["waiting"] == {_intake(env, "Waits")["task_id"]: ["nothing-landed"]}
    assert doc["held"] == [{"id": parked["task_id"], "reason": ""}]


def test_status_json_with_nothing_queued_keeps_the_bare_rows_list(env, capsys):
    assert main(["route", "status", "--profile", env["profile"], "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


@pytest.mark.parametrize(("flags", "priority"), [(("--set", "5"), 5), (("--set", "-2"), -2)])
def test_priority_on_an_intake_id_writes_priority_to_the_file(env, capsys, flags, priority):
    assert _file(env, "Fix the thing", "--intake") == 0
    row = _intake(env, "Fix the thing")
    capsys.readouterr()

    assert main(["route", "priority", row["task_id"], *flags, "--profile", env["profile"]]) == 0

    assert int(_fields(env, row)["priority"]) == priority
    assert capsys.readouterr().out.splitlines()[-1] == f"{row['task_id']}: priority {priority} on 1 item(s)"


def test_priority_top_on_an_intake_id_passes_the_others(env, capsys):
    assert _file(env, "First", "--intake") == 0
    assert _file(env, "Second", "--intake") == 0
    first, second = _intake(env, "First"), _intake(env, "Second")
    assert main(["route", "priority", first["task_id"], "--set", "4", "--profile", env["profile"]]) == 0

    assert main(["route", "priority", second["task_id"], "--top", "--profile", env["profile"]]) == 0

    assert int(_fields(env, second)["priority"]) == 5


def test_priority_on_an_initiative_id_still_orders_the_initiative(env, capsys):
    phase = env["ws"] / "work" / "demo" / "1-build"
    phase.mkdir(parents=True)
    (phase / "a.md").write_text("---\nstate: ready\ntitle: T\n---\nBody.\n")
    env["rows"][("demo", "a")] = {"kind": "task", "initiative": "demo", "task_id": "a", "state": "ready", "priority": 0}

    assert main(["route", "priority", "demo", "--set", "3", "--profile", env["profile"]]) == 0

    assert "priority: 3" in (phase / "a.md").read_text()
