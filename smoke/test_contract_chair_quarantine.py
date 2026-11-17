"""Contract: the chair's revert of a phase <-> `python -m harness.store_cli set-state ... quarantined` and the queue upsert.

When main goes red after a land the chair reverts it and quarantines the tasks that land carried (approved or done)
through the store CLI, each by a compare-and-set on the state it read, then marks each row with the cause, so no
relaunch stacks onto a red main and the next tick reads the quarantine as main_red. The CLI once refused `quarantined`
and the chair dropped the non-zero exit, so the store never held the quarantine. This runs `chair_exec.perform` on a
revert_land with the revert fields `cox chair run` wires (`cli._revert_land_deps`) against the real store: the revert
itself fails for real (its repo does not exist), and the quarantine still has to land, or be named in the needs_chair.
"""

import json

from smoke import plan

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
    runs_dir, {holder!r}, chair_revert_ports.bounded(chair_exec.run_argv), lambda: [], recorded.append
)
action = {{
    "kind": "revert_land", "initiative": "ship", "phase": "p1", "repo": {repo!r}, "pr": 7, "commit": "abc123",
    "reason": "smoke: main went red", "epoch": 0,
}}
results = chair_exec.perform([action], dataclasses.replace(deps, **revert), lambda: 0, False)
print(json.dumps(results[0], default=str))
"""
# The reverted phase: two tasks its land carried, one dropped, one ready to build. Only the first two are quarantined.
TASKS = {"t1": "approved", "t2": "done", "t3": "dropped", "t4": "ready"}


def _seed(sandbox):
    for task, state in TASKS.items():
        fields = {"state": state, "title": f"Task {task}", "id": task, "phase": "p1", "repo": "app"}
        sandbox.write(f"work/ship/p1/{task}.md", plan.work_item_text(fields, "Task body"))
    imported = sandbox.cox("route", "import")
    assert imported.returncode == 0, imported.stdout + imported.stderr
    assert {t: sandbox.store_row("ship", t)["state"] for t in TASKS} == TASKS


def _revert(sandbox, holder="chair") -> dict:
    code = PERFORM.format(workspace=str(sandbox.workspace), repo=str(sandbox.home / "src" / "missing"), holder=holder)
    return sandbox.python_json(code)


def test_a_reverted_phase_quarantines_the_tasks_its_land_carried_and_marks_the_cause(sandbox):
    _seed(sandbox)

    result = _revert(sandbox)

    assert result["needs_chair"]["cause"] == "revert_failed", json.dumps(result)
    assert "quarantine not recorded" not in result["needs_chair"]["reason"]
    assert {t: sandbox.store_row("ship", t)["state"] for t in TASKS} == {**TASKS, "t1": "quarantined", "t2": "quarantined"}
    marks = {t: (sandbox.store_row("ship", t).get("extra") or {}).get("quarantine_mark") for t in TASKS}
    assert marks == {
        "t1": {"cause": "main_red", "reason": "smoke: main went red", "run": ""},
        "t2": {"cause": "main_red", "reason": "smoke: main went red", "run": ""},
        "t3": None,
        "t4": None,
    }


def test_a_quarantine_the_store_does_not_take_is_named_in_the_needs_chair(sandbox):
    # The store CLI refuses a write with an empty `--by` (exit 2), so every quarantine write fails for real.
    _seed(sandbox)

    result = _revert(sandbox, holder="")

    reason = result["needs_chair"]["reason"]
    for task in ("t1", "t2"):
        assert f"quarantine not recorded: warning: store did not record ship/{task} as quarantined: exit 2" in reason, reason
    assert {t: sandbox.store_row("ship", t)["state"] for t in TASKS} == TASKS
