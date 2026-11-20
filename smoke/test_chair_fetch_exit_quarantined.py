"""The chair's `fetch_exit` does not read a quarantined run record as an approval.

A remote run's records are on disk under `runs/r1/tasks/p1/`. `t1` was approved by review and then quarantined when its
checks failed; `t2` was approved and is clean. Both tickets sit at `state: ready` and both store rows are `ready`.
`perform_fetch_exit` runs over the real runs directory, ticket files and store. Before the fix it moved `t1` to
approved in the file and mirrored `approved` to the store row, overwriting the operator's `ready`. The fetch itself
(`cox runs fetch`, an ssh pull) is the one door the test closes: its exit code is 0 and the records are already on disk.
"""

import json

import pytest

from smoke import plan

pytest.importorskip("psycopg")

SEED = """import json
from harness import store_api
for task in ("t1", "t2"):
    store_api.set_work_state({url!r}, "ship", task, "ready", "smoke", "2026-11-17T00:00:00Z", phase="p1")
print(json.dumps("seeded"))
"""

FETCH_EXIT = """import json
from pathlib import Path
from agent_tools import chair_exec_land
workspace = Path({workspace!r})
deps = chair_exec_land.Deps(
    run=lambda argv: (0, ""),
    delete_branches=lambda repo, pattern: ([], ""),
    acquire_lease=lambda *args, **kwargs: "",
    record=lambda entry: None,
    run_id=lambda action: "",
    repo_for=lambda action: "",
    runs_dir=workspace / "runs",
    work_dir=workspace,
)
result = chair_exec_land.perform_fetch_exit({{"kind": "fetch_exit", "run": "r1", "initiative": "ship"}}, deps)
print(json.dumps({{"status": result["status"], "reason": result["reason"]}}))
"""


def _record(task: str, **extra: object) -> str:
    base = {"run": "r1", "task": task, "phase": "p1", "branch": f"epic/ship/p1--{task}", "landed": False,
            "review": {"verdict": "approve"}, "arbitration": "arbiter: skipped (both approved)"}
    return json.dumps({**base, **extra})


def _stage(sandbox) -> None:
    sandbox.write("work/ship/initiative.md", plan.work_item_text({"title": "ship", "repo": "~/src/app", "draft": "false"}, "An initiative."))
    for task in ("t1", "t2"):
        sandbox.write(f"work/ship/p1/{task}.md", plan.work_item_text({"id": task, "title": task, "phase": "p1", "state": "ready"}, "Body."))
    sandbox.write("runs/r1/tasks/p1/t1.json", _record("t1", status="quarantined", cause="code"))
    sandbox.write("runs/r1/tasks/p1/t2.json", _record("t2"))
    assert sandbox.python_json(SEED.format(url=sandbox.store_url)) == "seeded"


def test_fetch_exit_leaves_a_quarantined_task_ready_in_file_and_store(sandbox):
    _stage(sandbox)
    done = sandbox.python_json(FETCH_EXIT.format(workspace=str(sandbox.workspace)))
    assert done == {"status": "done", "reason": "fetched r1; approved: t2"}, done
    quarantined = (sandbox.workspace / "work/ship/p1/t1.md").read_text(encoding="utf-8")
    assert "state: ready" in quarantined and "state: approved" not in quarantined, quarantined
    assert sandbox.store_row("ship", "t1")["state"] == "ready", sandbox.store_rows()


def test_fetch_exit_still_approves_a_clean_sibling_in_file_and_store(sandbox):
    _stage(sandbox)
    sandbox.python_json(FETCH_EXIT.format(workspace=str(sandbox.workspace)))
    clean = (sandbox.workspace / "work/ship/p1/t2.md").read_text(encoding="utf-8")
    assert "state: approved" in clean, clean
    assert sandbox.store_row("ship", "t2")["state"] == "approved", sandbox.store_rows()
