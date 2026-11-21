"""L5, the local merge agent, end to end: two parallel tasks of one phase add a line at the same place in one function,
so the second's change does not apply onto the phase tip once the first has rolled up. The lane merges them on the
worker, runs both tasks' cheap checks and `verify` commands on the merge, and lands it as the second task's one commit;
or, when the merge fails them, builds the second task again on the new tip. Either way one `collision` row names the
two tasks and the file. A third case is the roll-up's other guard: a task a stale lane already landed on the tip is
adopted, never applied twice. Real Postgres, a local bare origin, the real `cox worker` and `cox chair`; the scripted
runner answers the `merge_tasks` seat as it answers every other.

The phase and its machine are `smoke/test_phase_lane.py`'s (`lane_machine`): t1 and t2 independent, t3 needing both.
Here `init/ship` also holds `app.py`, whose `greet` both t1 and t2 change: each adds a line right after the `def`. t1's
check sleeps 1 s, t2's 3 s, so t1 rolls up first and t2 is the one that collides.
"""

from __future__ import annotations

import json

import pytest

from smoke import plan
from smoke.test_phase_lane import (
    BRANCH,
    INIT,
    INITIATIVE,
    PHASE,
    Worker,
    _owner,
    _row,
    _run_log,
    _steps,
    _until,
    lane_machine,
)

__all__ = ["lane_machine"]  # the fixture, imported from the phase lane's smoke, which pytest finds by name here

DEF, RETURN = "def greet(name):\n", '    return "hello " + name\n'
APP = DEF + RETURN
T1_LINE, T2_LINE = "    name = name.strip()\n", "    name = name.title()\n"
BOTH = DEF + T1_LINE + T2_LINE + RETURN
BROKEN = DEF + T1_LINE + T2_LINE + '    return "hello " + name  # BROKEN\n'
# t1's own `verify` command: a merge that writes BROKEN fails it, and only the merge does.
NOT_BROKEN = "! grep -q BROKEN app.py"


def _insert(after: str, line: str, before: str = RETURN) -> str:
    """A unified diff that adds `line` to `app.py` between `after` and `before`, the file's whole text."""
    return (f"diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
            f"@@ -1,{len((after + before).splitlines())} +1,{len((after + line + before).splitlines())} @@\n"
            + "".join(f" {x}\n" for x in after.splitlines()) + f"+{line}" + "".join(f" {x}\n" for x in before.splitlines()))


def _build(task: str, wait_s: int, patch: str = "") -> dict[str, object]:
    files = [f"notes/{task}.txt", f"waits/{task}", *(["app.py"] if patch else [])]
    body = patch + plan.new_file_patch(f"notes/{task}.txt", task) + plan.new_file_patch(f"waits/{task}", str(wait_s))
    return {"patch": body, "summary": "built", "files_touched": files, "commands_run": []}


def _merge_answer(text: str) -> dict[str, object]:
    return {"resolved": True, "files": [{"path": "app.py", "content": text}], "summary": "kept both edits"}


def _answers(merged: str, rebuild: dict[str, object] | None = None) -> dict[str, object]:
    """Plan and build only, as the phase lane's smoke scripts them, plus the `merge_tasks` seat's answer for t2; with
    `rebuild`, t2's second build (the rebuild on the new tip) answers that."""
    t2 = _build("t2", 3, _insert(DEF, T2_LINE))
    return {
        "plan": {"steps": ["edit greet"], "files_expected": [], "out_of_scope": []},
        "build@t1": _build("t1", 1, _insert(DEF, T1_LINE)),
        "build@t2": [t2, rebuild] if rebuild else t2,
        "build@t3": _build("t3", 1),
        "merge_tasks@t2": _merge_answer(merged),
    }


def _commits(sandbox, origin) -> list[dict[str, str]]:
    """The phase branch's commits on origin since `init/ship`, oldest first: each one's task, run and
    `Cox-Merged-With`."""
    fmt = ("%H%x1f%(trailers:key=Cox-Task,valueonly,separator=)%x1f%(trailers:key=Cox-Run,valueonly,separator=)%x1f"
           "%(trailers:key=Cox-Merged-With,valueonly,separator=)%x1f.%x1e")
    text = sandbox.git(origin, "log", "--reverse", f"--format={fmt}", f"{INIT}..{BRANCH}")
    # The `.` field ends each record: `str.strip` takes \x1e and \x1f for whitespace, so an empty last field would go.
    records = [r.strip("\n").split("\x1f") for r in text.split("\x1e") if r.strip("\n")]
    return [{"sha": f[0].strip(), "task": f[1].strip(), "run": f[2].strip(), "merged_with": f[3].strip()} for f in records]


def _collisions(sandbox, run_id: str) -> list[dict]:
    rows = _owner(sandbox, "SELECT task_id, ok, verdict, result_json FROM step_results WHERE run_id = %s AND kind = "
                           "'collision'", args=(run_id,))[0]
    return [{"task": t, "ok": ok, "verdict": v, "result": json.loads(r) if isinstance(r, str) else r}
            for t, ok, v, r in rows]


def _finished(sandbox, env, name) -> tuple[dict, str]:
    worker = Worker(sandbox, env, "once", "--name", name)
    try:
        out = worker.finished(timeout=300)
    finally:
        worker.kill()
    row = _row(sandbox)
    log = _run_log(sandbox, row["run_id"])
    assert (row["state"], row["attempts"]) == ("built", 0), (row, out, log)
    return row, log


@pytest.fixture
def merge_machine(lane_machine):
    """The phase lane's machine with `app.py` on `init/ship` and the `merge_tasks` seat bound to a smoke skill."""
    sandbox, repo, name, env, _, launchable = lane_machine
    (repo.path / "app.py").write_text(APP, encoding="utf-8")
    sandbox.git(repo.path, "add", "app.py")
    sandbox.git(repo.path, "commit", "--quiet", "-m", "app")
    sandbox.git(repo.path, "push", "--quiet", "origin", f"HEAD:refs/heads/{INIT}")
    path = launchable.cartridges / "smoke" / "cartridge.yaml"
    cartridge = json.loads(path.read_text(encoding="utf-8"))
    cartridge["skills"]["merge_tasks"] = "smoke-skills:merge-tasks"
    path.write_text(json.dumps(cartridge, indent=2) + "\n", encoding="utf-8")
    skill = launchable.skills / "skills" / "merge-tasks" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("---\nname: merge-tasks\ndescription: smoke\n---\n\nThe merge-tasks skill.\n", encoding="utf-8")
    return sandbox, repo, name, env, sandbox.git(repo.origin, "rev-parse", INIT), launchable


def test_two_tasks_that_edit_one_function_are_merged_on_the_worker_and_land_as_two_commits(merge_machine):
    sandbox, repo, name, env, init_sha, launchable = merge_machine
    launchable.scripted(_answers(BOTH))
    row, log = _finished(sandbox, env, name)
    run_id = row["run_id"]

    commits = _commits(sandbox, repo.origin)
    by_task = {c["task"]: c for c in commits}
    assert sorted(c["task"] for c in commits) == ["t1", "t2", "t3"], commits  # one commit per task, t1 and t2 included
    assert (by_task["t1"]["merged_with"], by_task["t2"]["merged_with"]) == ("", "t1"), commits
    assert sandbox.git(repo.origin, "show", f"{BRANCH}:app.py") + "\n" == BOTH  # both edits kept
    builds = sorted(s["task_id"] for s in _steps(sandbox, run_id) if s["kind"] == "build")
    assert builds == ["t1", "t2", "t3"], _steps(sandbox, run_id)  # no rebuild ran
    collided = _collisions(sandbox, run_id)
    assert [(c["task"], c["verdict"], c["result"]["other"], c["result"]["files"]) for c in collided] == [
        ("t2", "commit", "t1", ["app.py"])], collided
    assert collided[0]["result"]["failed_checks"] == {"t1": [], "t2": []}, collided  # both sides ran checks, all passed
    assert "lane: t2 collided with t1 on app.py; next: commit" in log
    assert sandbox.git(repo.origin, "rev-parse", INIT) == init_sha  # the lane never moves the initiative branch


def test_a_merge_that_fails_the_other_tasks_verify_is_dropped_and_the_later_task_rebuilt_on_the_new_tip(merge_machine):
    """t1's own `verify` command fails on the merge (which writes BROKEN) and on nothing else: the merge is dropped
    only because the other side's verification runs on the merged tree."""
    sandbox, repo, name, env, init_sha, launchable = merge_machine
    sandbox.write(f"work/{INITIATIVE}/{PHASE}/t1.md", plan.work_item_text(
        {"id": "t1", "title": "Add note t1", "state": "ready", "phase": PHASE, "repo": "app",
         "verify": json.dumps(NOT_BROKEN)}, "Add notes/t1.txt."))
    imported = sandbox.cox("route", "import")  # the store holds the item; the work files are exported from it
    assert imported.returncode == 0, imported.stdout + imported.stderr
    rebuilt = _build("t2", 1, _insert(DEF + T1_LINE, T2_LINE))
    launchable.scripted(_answers(BROKEN, rebuild=rebuilt))
    row, log = _finished(sandbox, env, name)
    run_id = row["run_id"]

    commits = _commits(sandbox, repo.origin)
    assert sorted(c["task"] for c in commits) == ["t1", "t2", "t3"], commits
    assert all(c["merged_with"] == "" for c in commits), commits  # the merge was dropped: t2 is its rebuild
    assert sandbox.git(repo.origin, "show", f"{BRANCH}:app.py") + "\n" == BOTH
    steps = _steps(sandbox, run_id)
    assert sorted(s["step"] for s in steps if s["task_id"] == "t2" and s["kind"] == "build") == [
        "task/t2-rebuild/build", "task/t2/build"], steps
    collided = _collisions(sandbox, run_id)
    assert [(c["task"], c["ok"], c["verdict"], c["result"]["files"]) for c in collided] == [
        ("t2", 0, "rebuild", ["app.py"])], collided
    assert collided[0]["result"]["failed_checks"] == {"t1": ["verify-1"], "t2": []}, collided
    assert log.index("next: rebuild") < log.index("lane: rebuilding t2") < log.index("lane: t2 rolled up"), log
    assert sandbox.git(repo.origin, "rev-parse", INIT) == init_sha
    assert _owner(sandbox, "SELECT count(*) FROM step_results WHERE run_id = %s AND kind = 'rollup' AND task_id = 't2'",
                  args=(run_id,))[0] == [[1]]  # one roll-up of t2: the dropped merge wrote none


def test_a_task_a_stale_lane_already_landed_on_the_tip_is_adopted_not_applied_twice(merge_machine):
    """While t1 and t2 are in their checks, a stale lane's commit of t2 lands on origin (played by the test). t1's
    push is refused, and t1 is re-applied onto origin's tip, which now holds t2; when t2 rolls up, the tip's
    trailers already carry it: the lane adopts that commit, with its roll-up row, and applies nothing."""
    sandbox, repo, name, env, init_sha, launchable = merge_machine
    launchable.scripted({**_answers(BOTH), "build@t1": _build("t1", 4), "build@t2": _build("t2", 8)})
    worker = Worker(sandbox, env, "once", "--name", name)
    try:
        _until(lambda: {s["task_id"] for s in _steps(sandbox) if s["kind"] == "build"} >= {"t1", "t2"}, 240,
               "t1's and t2's builds")
        other = sandbox.home / "stale"
        sandbox.git(repo.path.parent, "clone", "--quiet", "--branch", INIT, str(repo.origin), str(other))
        for path, text in ((other / "notes" / "t2.txt", "t2\n"), (other / "waits" / "t2", "8\n")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        sandbox.git(other, "add", "-A")
        sandbox.git(other, "commit", "--quiet", "-m",
                    f"epic {INITIATIVE}.p1-9: t2\n\nAdd note t2\n\nCox-Initiative: {INITIATIVE}\nCox-Phase: {PHASE}\n"
                    f"Cox-Task: t2\nCox-Run: {INITIATIVE}.p1-9\nCox-Kind: task\n")
        sandbox.git(other, "push", "--quiet", "origin", f"HEAD:refs/heads/{BRANCH}")
        stale = sandbox.git(other, "rev-parse", "HEAD")
        out = worker.finished(timeout=300)
    finally:
        worker.kill()
    row = _row(sandbox)
    run_id = row["run_id"]
    log = _run_log(sandbox, run_id)
    assert (row["state"], row["attempts"]) == ("built", 0), (row, out, log)
    assert f"lane: t2 is already on {BRANCH} as {stale[:12]}; adopted, not applied again" in log, log
    commits = _commits(sandbox, repo.origin)
    assert [(c["task"], c["run"]) for c in commits] == [
        ("t2", f"{INITIATIVE}.p1-9"), ("t1", run_id), ("t3", run_id)], commits  # t2 once: the stale lane's
    steps = {(s["task_id"], s["kind"]): s for s in _steps(sandbox, run_id)}
    assert steps[("t2", "rollup")]["subject_sha"] == stale, steps
    assert not _collisions(sandbox, run_id)
    assert sandbox.git(repo.origin, "rev-parse", INIT) == init_sha
