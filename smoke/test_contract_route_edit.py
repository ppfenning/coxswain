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


def _paths(value):
    """Every string in a JSON value that is an absolute path."""
    if isinstance(value, dict):
        return [p for v in value.values() for p in _paths(v)]
    if isinstance(value, list):
        return [p for v in value for p in _paths(v)]
    return [value] if isinstance(value, str) and value.startswith("/") else []


def test_route_context_counts_a_ready_task_from_the_store_under_the_sandbox_profile(sandbox):
    _imported(sandbox, {**TASK, "state": "ready"})
    done = sandbox.cox("route", "context", "--json")
    assert done.returncode == 0, done.stderr
    context = json.loads(done.stdout)
    assert {"id": "ship", "phase": "p1", "ready": 1} in context["initiatives"]
    # cox resolved the profile the sandbox HOME names, and every path it reports is the sandbox's or the install's.
    # That does not prove it opens no other file; the environment (plan.sandbox_env: no real HOME, XDG root or
    # AGENT_TOOLS_PROFILE) is what keeps the real home out of its reach.
    assert (context["team"], context["workspace_dir"]) == ("smoke", str(sandbox.workspace))
    assert context["harness_dir"] == str(sandbox.install.root)
    assert context["provider_profile"] == str(sandbox.home / ".config" / "agent-tools" / "provider.yaml")
    roots = (str(sandbox.home.parent), str(sandbox.install.root))
    assert [p for p in _paths(context) if not p.startswith(roots)] == []


def test_route_edit_with_the_initiative_moves_that_initiatives_ticket_when_the_id_repeats(sandbox):
    # The inbox remedy names the initiative (H1 review): `t1` in two initiatives must move only the named one.
    ship = _imported(sandbox, TASK)
    other = sandbox.write("work/dock/p1/t1.md", plan.work_item_text(TASK, "Task body"))
    imported = sandbox.cox("route", "import")
    assert imported.returncode == 0, imported.stdout + imported.stderr
    bare = sandbox.cox("route", "edit", "t1", "--state", "ready")
    assert bare.returncode == 2 and "--initiative" in bare.stdout, bare.stdout + bare.stderr
    named = sandbox.cox("route", "edit", "t1", "--initiative", "dock", "--state", "ready")
    assert named.returncode == 0, named.stdout + named.stderr
    assert sandbox.store_row("dock", "t1")["state"] == "ready"
    assert sandbox.store_row("ship", "t1")["state"] == "todo"
    assert plan.front_matter(other.read_text(encoding="utf-8"))["state"] == "ready"
    assert plan.front_matter(ship.read_text(encoding="utf-8"))["state"] == "todo"
