"""Contract: `cox route import` and `cox route edit` <-> the store, through `harness.store_queue` and
`harness.store_cli set-state`.

The unit tests of route edit replace the store with a double (`_Fake` in src/tools/tests/test_route_edit_task.py), so
a change to what the store CLIs accept, or to the argv cox builds for them, passes there. Here the file, the store row
and `cox route drift` must agree after every move. A move the store refuses (#1174's "invalid choice") fails here.
"""

import json

from smoke import plan

TASK = {"state": "todo", "title": "First task", "id": "t1", "phase": "p1", "repo": "app"}
ITEM = "work/ship/p1/t1.md"


def _imported(sandbox, fields):
    path = sandbox.write(ITEM, plan.work_item_text(fields, "Task body"))
    first = sandbox.cox("route", "import")
    assert first.returncode == 0, first.stdout + first.stderr
    assert "written 1," in first.stdout
    return path


def _agree(sandbox, path, state):
    assert plan.front_matter(path.read_text(encoding="utf-8"))["state"] == state
    assert sandbox.store_row("ship", "t1")["state"] == state
    drift = sandbox.cox("route", "drift")
    assert drift.returncode == 0, drift.stdout + drift.stderr
    assert "no drift" in drift.stdout


def test_route_import_is_idempotent_against_the_store(sandbox):
    path = _imported(sandbox, TASK)
    again = sandbox.cox("route", "import")
    assert again.returncode == 0, again.stdout + again.stderr
    assert "written 0, unchanged 1" in again.stdout
    _agree(sandbox, path, "todo")


def test_route_edit_moves_a_task_forward_through_the_store(sandbox):
    path = _imported(sandbox, TASK)
    for state, extra in (("ready", ()), ("approved", ()), ("done", ("--pr", "https://example.invalid/app/pull/1"))):
        done = sandbox.cox("route", "edit", "t1", "--state", state, *extra)
        assert done.returncode == 0, f"--state {state}: {done.stdout}{done.stderr}"
        _agree(sandbox, path, state)
    assert sandbox.store_row("ship", "t1")["updated_by"] == "cox route edit"


def test_route_edit_refuses_a_backward_move_and_writes_nothing(sandbox):
    path = _imported(sandbox, {**TASK, "state": "approved"})
    done = sandbox.cox("route", "edit", "t1", "--state", "ready")
    assert done.returncode == 2, done.stdout + done.stderr
    _agree(sandbox, path, "approved")


def test_route_context_counts_a_ready_task_from_the_store(sandbox):
    _imported(sandbox, {**TASK, "state": "ready"})
    context = sandbox.cox("route", "context", "--json")
    assert context.returncode == 0, context.stderr
    assert {"id": "ship", "phase": "p1", "ready": 1} in json.loads(context.stdout)["initiatives"]
