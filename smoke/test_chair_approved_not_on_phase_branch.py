"""An approved task whose latest record was quarantined for cause `code`, and whose commit never reached
`epic/ship/p1`, is stranded: `cox runs stranded` lists it and the chair's stranded read hands it to the planner.

Before the fix the code quarantine made `runs_stranded` treat the approval as superseded and nothing else read the
task, so the listing said "no stranded work" and the chair's needs-chair path saw no row.
"""

import json

from smoke import plan

READ = """import json
from pathlib import Path
from agent_tools import chair_read_stranded
from agent_tools.cli_runs import _stranded_inputs
records, items = _stranded_inputs(Path({runs!r}))
print(json.dumps(chair_read_stranded.read_stranded(records, items)))
"""


def _stage(sandbox, repo):
    sandbox.git(repo.path, "branch", "epic/ship/p1")  # the phase branch, at main: no commit of the task on it
    sandbox.write("work/ship/initiative.md", plan.work_item_text({"title": "ship", "repo": "~/src/app", "draft": "false"}, "An initiative."))
    sandbox.write("work/ship/p1/t1.md", plan.work_item_text({"id": "t1", "title": "T", "phase": "p1", "state": "approved"}, "Body."))
    record = {"run": "r1", "task": "t1", "phase": "p1", "branch": "epic/ship/p1--t1", "repo": "~/src/app", "landed": False,
              "review": {"verdict": "approve"}, "arbitration": "arbiter: skipped (both approved)", "cause": "code"}
    sandbox.write("runs/r1/tasks/p1/t1.json", json.dumps(record))


def test_runs_stranded_lists_an_approved_task_absent_from_its_phase_branch(sandbox, repo):
    _stage(sandbox, repo)
    done = sandbox.cox("runs", "stranded", "--json")
    assert done.returncode == 0, done.stdout + done.stderr
    rows = json.loads(done.stdout)
    assert [(r["task"], r["branch"]) for r in rows] == [("t1", "epic/ship/p1")], done.stdout
    assert "runs land" not in rows[0]["remedy"], rows


def test_the_chair_stranded_read_carries_the_same_row(sandbox, repo):
    _stage(sandbox, repo)
    rows = sandbox.python_json(READ.format(runs=str(sandbox.workspace / "runs")))
    assert [r["task"] for r in rows] == ["t1"], rows


def test_a_task_whose_commit_is_on_the_phase_branch_is_not_listed(sandbox, repo):
    _stage(sandbox, repo)
    sandbox.git(repo.path, "checkout", "--quiet", "epic/ship/p1")
    (repo.path / "t1.txt").write_text("t1\n", encoding="utf-8")
    sandbox.git(repo.path, "add", "t1.txt")
    sandbox.git(repo.path, "commit", "--quiet", "-m", "t1")
    sandbox.git(repo.path, "checkout", "--quiet", "main")
    done = sandbox.cox("runs", "stranded", "--json")
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(done.stdout) == [], done.stdout
