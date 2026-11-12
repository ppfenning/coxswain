"""A direct phase or task land is refused while a parent phase it needs has not landed."""

import json
import subprocess as sp

import pytest

from agent_tools import cli

WAIT = "waiting on parent land: p1"


def _ticket(work, phase, task, state="approved", needs=()):
    d = work / "x" / phase
    d.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"id: {task}", f"phase: {phase}", f"state: {state}", f"needs: [{', '.join(needs)}]", "---", ""]
    (d / f"{task}.md").write_text("\n".join(lines), encoding="utf-8")


def _work(tmp_path, p1_state="approved"):
    work = tmp_path / "work"
    _ticket(work, "p1", "t1", p1_state)
    _ticket(work, "p2", "t2", needs=("t1",))
    return work


def _git(repo, *args):
    return sp.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e", *args],
                  check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    origin, root = tmp_path / "origin.git", tmp_path / "repo"
    sp.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    root.mkdir()
    sp.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    (root / "f").write_text("x")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    for phase, task in (("p1", "t1"), ("p2", "t2")):
        for branch in (f"epic/x/{phase}", f"agents/epic-x-5/{task}"):
            _git(root, "checkout", "-qb", branch, "main")
            (root / f"{task}.txt").write_text(task)
            _git(root, "add", "-A")
            _git(root, "commit", "-qm", f"{task} work")
    _git(root, "checkout", "-q", "main")
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "-q", "origin", "--all")
    return root


def _records(tmp_path, land_at="task"):
    runs = tmp_path / "runs"
    for phase, task in (("p1", "t1"), ("p2", "t2")):
        (runs / "epic-x-5" / "tasks" / phase).mkdir(parents=True, exist_ok=True)
        record = {"run": "epic-x-5", "task": task, "phase": phase, "initiative": "x", "title": task, "repo": "wire",
                  "status": "approved", "proposals": [{"kind": "draft_pr_create", "title": task}], "review": {"verdict": "approve"}, "build": {"summary": "built"}}
        (runs / "epic-x-5" / "tasks" / phase / f"{task}.json").write_text(json.dumps(record), encoding="utf-8")
        (runs / f"epic-x-5:{phase}.json").write_text(
            json.dumps({"run_id": f"epic-x-5:{phase}", "run": "epic-x-5", "phase": phase, "initiative": "x", "last_phase": phase == "p2", "land_at": land_at}),
            encoding="utf-8")
    return runs


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(cli, "_open_prs_for", lambda _repo, _branch, _forge=None: [])
    monkeypatch.setattr(cli, "_phase_merged_pr", lambda *_args: None)


def _land(repo, runs, *extra):
    return cli.main(["runs", "land", "epic-x-5", "--repo", str(repo), "--runs-dir", str(runs), "--gate", "full", *extra])


def _refusals(out):
    return [s for s in json.loads(out[out.index("["):]) if s["kind"] == "refuse"]


def test_a_child_phase_land_is_refused_naming_the_parent(repo, tmp_path, capsys):
    _work(tmp_path)
    assert _land(repo, _records(tmp_path), "--phase", "p2") == 2
    assert _refusals(capsys.readouterr().out) == [{"kind": "refuse", "reason": WAIT}]


def test_the_parent_phase_land_plans_normally(repo, tmp_path, capsys):
    _work(tmp_path)
    rc = _land(repo, _records(tmp_path), "--phase", "p1")
    out = capsys.readouterr().out
    assert _refusals(out) == [] and rc == 0, out


def test_a_child_phase_plans_once_the_parent_tasks_are_done(repo, tmp_path, capsys):
    _work(tmp_path, p1_state="done")
    rc = _land(repo, _records(tmp_path), "--phase", "p2")
    out = capsys.readouterr().out
    assert _refusals(out) == [] and rc == 0, out


def test_apply_refuses_the_child_phase_with_the_same_text_and_code(repo, tmp_path, capsys):
    _work(tmp_path)
    assert _land(repo, _records(tmp_path), "--phase", "p2", "--apply") == 2
    assert f"land: refusing, {WAIT}" in capsys.readouterr().out


def test_a_child_task_land_is_refused_naming_the_parent(repo, tmp_path, capsys):
    _work(tmp_path)
    assert _land(repo, _records(tmp_path), "--task", "t2") == 2
    assert _refusals(capsys.readouterr().out) == [{"kind": "refuse", "reason": WAIT}]


def test_apply_refuses_the_child_task_with_the_same_text_and_code(repo, tmp_path, capsys):
    _work(tmp_path)
    assert _land(repo, _records(tmp_path), "--task", "t2", "--apply") == 2
    assert f"land: refusing, {WAIT}" in capsys.readouterr().out


def test_a_task_without_cross_phase_needs_is_never_refused(repo, tmp_path, capsys):
    _work(tmp_path)
    rc = _land(repo, _records(tmp_path), "--task", "t1")
    out = capsys.readouterr().out
    assert _refusals(out) == [] and rc == 0, out


def test_a_ticket_land_carries_the_earlier_phases_so_the_child_is_not_refused(repo, tmp_path, capsys):
    _work(tmp_path)
    rc = _land(repo, _records(tmp_path, land_at="ticket"), "--phase", "p2")
    out = capsys.readouterr().out
    assert WAIT not in out and rc == 0, out


def test_an_already_merged_phase_is_not_refused(repo, tmp_path, monkeypatch, capsys):
    _work(tmp_path)
    monkeypatch.setattr(cli, "_phase_merged_pr", lambda *_args: {"number": 7, "state": "merged"})
    _land(repo, _records(tmp_path), "--phase", "p2")
    assert WAIT not in capsys.readouterr().out


def test_the_refusal_is_the_one_step_for_tasks_whose_need_is_unlanded():
    facts = ({"t1": "p1", "t2": "p2"}, {"t2": ["t1"]}, set())
    assert cli._parent_land_refusal(["t2"], facts) == [{"kind": "refuse", "reason": WAIT}]
    assert cli._parent_land_refusal(["t1"], facts) is None
    assert cli._parent_land_refusal(["t2"], ({**facts[0]}, facts[1], {"t1"})) is None
    assert cli._parent_land_refusal(["t2"], facts, carried=["t1"]) is None


def test_landed_is_every_done_or_dropped_task_across_the_initiative(tmp_path):
    work = _work(tmp_path)
    _ticket(work, "p1", "t0", "dropped")
    phase_of, needs, landed = cli._initiative_land_facts(work, "x", tmp_path / "runs", "files")
    assert phase_of == {"t0": "p1", "t1": "p1", "t2": "p2"} and needs["t2"] == ["t1"] and landed == {"t0"}
