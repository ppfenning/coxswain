"""The `cox route` commands that write the work store, each run through the installed `cox` against a fresh store.

The unit tests of these commands replace the store and the files with doubles, so a wrong argument passed from the
command to a correct core passes there (adversarial review F1). Here the file and the store row must both show the
move: `route approve`, `route decline`, `route remove`, `route requeue`, and `route sync` through a registered tracker.
"""

import json
from dataclasses import replace

from smoke import plan

INITIATIVE = {"title": "Ship", "repo": "~/src/app", "draft": "true"}
TASK = {"id": "t1", "title": "First task", "phase": "p1", "state": "todo", "repo": "app"}
TICKET = "work/ship/p1/t1.md"


def _ok(done):
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def _draft(sandbox, initiative=INITIATIVE, task=TASK):
    """A draft initiative `ship` with one todo ticket, imported into the store."""
    sandbox.write("work/ship/initiative.md", plan.work_item_text(initiative, "An initiative."))
    ticket = sandbox.write(TICKET, plan.work_item_text(task, "Task body."))
    _ok(sandbox.cox("route", "import"))
    return ticket


def _state(path):
    return plan.front_matter(path.read_text(encoding="utf-8"))


def test_route_approve_moves_a_draft_todo_ticket_to_ready_in_the_file_and_the_store(sandbox):
    ticket = _draft(sandbox)
    out = _ok(sandbox.cox("route", "approve", "ship", "--by", "smoke"))
    assert "ship: t1 todo -> ready" in out, out
    assert _state(ticket)["state"] == "ready"
    assert sandbox.store_row("ship", "t1")["state"] == "ready"
    assert _state(sandbox.workspace / "work/ship/initiative.md")["draft"] == "false"


def test_route_decline_drops_a_draft_todo_ticket_in_the_file_and_the_store(sandbox):
    ticket = _draft(sandbox)
    out = _ok(sandbox.cox("route", "decline", "ship", "--reason", "not now", "--by", "smoke"))
    assert "ship: t1 todo -> dropped" in out, out
    assert _state(ticket)["state"] == "dropped"
    assert sandbox.store_row("ship", "t1")["state"] == "dropped"


def test_route_remove_files_a_queued_intake_as_done_in_the_folder_and_the_store(sandbox):
    sandbox.write("intake/later.md", plan.work_item_text({"title": "Later"}, "An idea for later."))
    _ok(sandbox.cox("route", "import"))
    out = _ok(sandbox.cox("route", "remove", "later", "--reason", "duplicate", "--by", "smoke"))
    assert "remove: intake later: moved to" in out, out
    assert not (sandbox.workspace / "intake/later.md").exists()
    moved = (sandbox.workspace / "intake/done/later.md").read_text(encoding="utf-8")
    assert "removed by smoke" in moved and "duplicate" in moved, moved
    row = next(r for r in sandbox.store_rows() if r.get("kind") == "intake")
    assert (row["task_id"], row["state"]) == ("later", "done"), row


def test_route_requeue_gives_a_declined_initiatives_intake_back_to_the_queue(sandbox):
    sandbox.write("intake/ship.md", plan.work_item_text({"title": "Ship", "initiative": "ship"}, "Ship it."))
    _draft(sandbox, initiative={**INITIATIVE, "intake": "intake/ship.md"}, task={**TASK, "state": "dropped"})
    _ok(sandbox.cox("route", "store-fill"))  # the initiative's own row, which the chair's decompose writes
    out = _ok(sandbox.cox("route", "requeue", "ship"))
    assert "requeue: intake ship queued, initiative ship dropped" in out, out
    assert not (sandbox.workspace / "work/ship/initiative.md").exists()
    assert "initiative" not in _state(sandbox.workspace / "intake/ship.md")
    again = _ok(sandbox.cox("route", "requeue", "ship"))
    assert "already requeued" in again, again


TRACKER = '''import json
from pathlib import Path


def sync(workspace, items, *, dry_run):
    Path({out!r}).write_text(json.dumps({{"workspace": workspace, "dry_run": dry_run,
                                          "items": [[i.id, i.state] for i in items]}}))
    return True, [f"smoke-file: {{len(items)}} items"]
'''


def test_route_sync_hands_the_store_items_to_a_registered_tracker(sandbox, tmp_path):
    # A tracker from the `coxswain.trackers` entry-point group, found on PYTHONPATH as an installed plugin is: sync
    # resolves it by name and drives its `sync` with the store's items, never `gh` (cli_route_sync._route_sync_registered).
    plugin, out = tmp_path / "plugin", tmp_path / "synced.json"
    (plugin / "smoke_tracker-0.dist-info").mkdir(parents=True)
    (plugin / "smoke_tracker-0.dist-info" / "METADATA").write_text("Metadata-Version: 2.1\nName: smoke-tracker\nVersion: 0\n", encoding="utf-8")
    (plugin / "smoke_tracker-0.dist-info" / "entry_points.txt").write_text("[coxswain.trackers]\nsmoke-file = smoke_tracker\n", encoding="utf-8")
    (plugin / "smoke_tracker.py").write_text(TRACKER.format(out=str(out)), encoding="utf-8")
    sandbox.write("runs/policy.tracker.json", json.dumps({"tracker": "smoke-file"}))
    # an owner/name slug: sync resolves each item's repo before any tracker sees it
    _draft(sandbox, initiative={**INITIATIVE, "repo": "acme/app"}, task={**TASK, "state": "ready", "repo": "acme/app"})
    with_plugin = replace(sandbox, env={**sandbox.env, "PYTHONPATH": str(plugin)})

    printed = _ok(with_plugin.cox("route", "sync"))
    assert "smoke-file: " in printed, printed
    synced = json.loads(out.read_text(encoding="utf-8"))
    assert synced["dry_run"] is False and synced["workspace"] == str(sandbox.workspace)
    assert ["t1", "ready"] in synced["items"], synced
