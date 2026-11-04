"""A land by task whose branch main already carries skips the empty cherry-pick: closing steps only, `landed via PR #n`."""
import subprocess

from agent_tools import cli

RECORD = {"run": "r", "task": "t", "phase": "p", "initiative": "i", "status": "approved", "build": {"files_touched": ["a.txt"]}}
PICK = {"kind": "cherry_pick", "branch": "agents/t", "onto": "pr/t", "from": "main"}


def git(where, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(where), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _commit(where, name, text, subject):
    (where / name).write_text(text + "\n")
    git(where, "add", name)
    git(where, "commit", "-qm", subject)


def _repo(tmp_path, main_subject="task work (#42)", extra=False):
    """A clone whose main holds a.txt under `main_subject`; branch agents/t holds the same a.txt, plus b.txt when `extra`."""
    origin, clone = tmp_path / "origin.git", tmp_path / "clone"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(clone)], check=True)
    git(clone, "remote", "add", "origin", str(origin))
    _commit(clone, "base.txt", "base", "base")
    git(clone, "checkout", "-qb", "agents/t")
    _commit(clone, "a.txt", "a", "a")
    if extra:
        _commit(clone, "b.txt", "b", "b")
    git(clone, "checkout", "-q", "main")
    _commit(clone, "a.txt", "a", main_subject)
    git(clone, "push", "-q", "origin", "main")
    return clone


def _steps(tmp_path):
    path = str(tmp_path / "runs" / "r" / "tasks" / "p" / "t.json")
    return [PICK, {"kind": "push", "branch": "pr/t"}, {"kind": "clean", "branch": "pr/t"},
            {"kind": "mark_done", "task": "t", "path": path}]


def _walk(monkeypatch, repo, steps, record=RECORD):
    ran = []

    def step(_repo, s, _forge=None):
        ran.append(s)
        return (s["kind"] != "cherry_pick"), "ok"

    monkeypatch.setattr(cli, "_execute_land_step", step)
    rc, reached, pr = cli._land_execute(repo, steps, steps, record, None, "merge", False, None)
    return rc, reached, pr, ran


def test_a_branch_main_carries_lands_by_its_closing_steps_and_records_the_pr(tmp_path, monkeypatch, capsys):
    rc, reached, pr, ran = _walk(monkeypatch, _repo(tmp_path), _steps(tmp_path))
    assert (rc, reached, pr) == (0, ["clean", "mark_done"], "#42")
    assert ran[-1]["pr"] == "#42"
    assert any(line.startswith("landed via PR #") for line in capsys.readouterr().out.splitlines())


def test_a_branch_with_real_changes_still_reaches_its_cherry_pick(tmp_path, monkeypatch):
    rc, reached, pr, _ran = _walk(monkeypatch, _repo(tmp_path, extra=True), _steps(tmp_path))
    assert (rc, reached[0], pr) == (1, "cherry_pick", "")


def test_no_pr_number_on_main_runs_the_cherry_pick_as_planned(tmp_path, monkeypatch):
    rc, reached, _pr, _ran = _walk(monkeypatch, _repo(tmp_path, main_subject="task work"), _steps(tmp_path))
    assert (rc, reached[0]) == (1, "cherry_pick")


def test_a_gather_step_with_a_task_key_is_left_alone(tmp_path, monkeypatch):
    steps = [{**PICK, "task": "t"}, *_steps(tmp_path)[1:]]
    _rc, reached, _pr, _ran = _walk(monkeypatch, _repo(tmp_path), steps)
    assert reached[0] == "cherry_pick"


def test_a_record_without_files_is_left_alone(tmp_path, monkeypatch):
    _rc, reached, _pr, _ran = _walk(monkeypatch, _repo(tmp_path), _steps(tmp_path), record={**RECORD, "build": {}})
    assert reached[0] == "cherry_pick"


def test_carried_walk_keeps_closing_steps_of_both_lists_and_seeds_the_pr():
    steps = [PICK, {"kind": "push"}, {"kind": "clean"}, {"kind": "mark_done"}]
    assert cli._carried_walk(steps, steps, 42) == (steps[2:], steps[2:], "#42")
    assert cli._carried_walk(steps[:2], steps, 42) is None
