"""Contract: the chair's revert of a phase <-> `python -m harness.store_cli set-state ... quarantined`.

When main goes red after a land the chair reverts it and quarantines every task of the phase through the store CLI, so
no relaunch stacks onto a red main. The CLI once refused `quarantined` and the chair dropped the non-zero exit, so the
store never held the quarantine. This runs `chair_exec.perform` on a revert_land with the revert fields `cox chair run`
wires (`cli._revert_land_deps`) against the real store: the revert itself fails for real (its repo does not exist), and
the quarantine still has to land, or be named in the needs_chair the chair raises.
"""

import json

from smoke import plan

PHASE_TASKS = ("t1", "t2")
# One action, run the way the chair loop runs it. The epoch matches the one `perform` reads, so nothing fences it.
PERFORM = """import dataclasses
import json
from pathlib import Path
from agent_tools import chair_exec, cli, commands  # noqa: F401  (cli imports commands first)
from agent_tools import chair_revert_ports

ws = Path({workspace!r})
runs_dir = ws / "runs"
recorded = []
deps = chair_exec.edge_deps(
    runs_dir, ws, "smoke", 1, run_id=lambda a: "", repo_for=lambda a: "", record=recorded.append, host="smoke",
    harness_python="",
)
revert = cli._revert_land_deps(
    runs_dir, ws, "store", "smoke", chair_revert_ports.bounded(chair_exec.run_argv), lambda: [], recorded.append
)
action = {{
    "kind": "revert_land", "initiative": "ship", "phase": "p1", "repo": {repo!r}, "pr": 7, "commit": "abc123",
    "reason": "smoke: main went red", "epoch": 0,
}}
results = chair_exec.perform([action], dataclasses.replace(deps, **revert), lambda: 0, False)
print(json.dumps(results[0], default=str))
"""


def _task(task_id: str) -> str:
    fields = {"state": "ready", "title": f"Task {task_id}", "id": task_id, "phase": "p1", "repo": "app"}
    return plan.work_item_text(fields, "Task body")


def _revert(sandbox) -> dict:
    code = PERFORM.format(workspace=str(sandbox.workspace), repo=str(sandbox.home / "src" / "missing"))
    return sandbox.python_json(code)


def test_a_reverted_phase_is_quarantined_in_the_store(sandbox):
    for task in PHASE_TASKS:
        sandbox.write(f"work/ship/p1/{task}.md", _task(task))
    imported = sandbox.cox("route", "import")
    assert imported.returncode == 0, imported.stdout + imported.stderr

    result = _revert(sandbox)

    assert result["needs_chair"]["cause"] == "revert_failed", json.dumps(result)
    assert "quarantine not recorded" not in result["needs_chair"]["reason"]
    assert [sandbox.store_row("ship", t)["state"] for t in PHASE_TASKS] == ["quarantined", "quarantined"]


def test_a_quarantine_the_store_does_not_take_is_named_in_the_needs_chair(sandbox):
    # t1 is in the store; t2 is a ticket file the store has no row for, so its set-state is refused with exit 3.
    sandbox.write("work/ship/p1/t1.md", _task("t1"))
    imported = sandbox.cox("route", "import")
    assert imported.returncode == 0, imported.stdout + imported.stderr
    sandbox.write("work/ship/p1/t2.md", _task("t2"))

    result = _revert(sandbox)

    reason = result["needs_chair"]["reason"]
    assert "quarantine not recorded: warning: store did not record ship/t2 as quarantined: refused" in reason, reason
    assert "ship/t1" not in reason
    assert sandbox.store_row("ship", "t1")["state"] == "quarantined"
    assert [r["task_id"] for r in sandbox.store_rows() if r["initiative"] == "ship"] == ["t1"]
