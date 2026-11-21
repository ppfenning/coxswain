"""L6, the phase review loop, end to end: the phase lane reviews its own phase once its smoke has passed, builds the one
revision task a `revise` round asks for, smokes and reviews again, and finishes on the round that approves or
quarantines it; the chair then approves or quarantines the phase's tasks. Real Postgres, a local bare origin, the real
`cox worker` and `cox chair`; the scripted runner stands in for the model, its phase reviewer keyed by round
(`review_phase@<phase>-review-<round>`), so a round the test did not expect fails the run.

The machine is `test_phase_lane.py`'s (`lane_machine`): three tasks t1, t2 and t3 (t3 needing both) of phase p1, each
adding `notes/<task>.txt`. The cases share it two to a test, since each run of the machine costs about a minute:
- a duplicated helper revised away in round 1, approved in round 2; then a task added to the approved phase, which the
  chair reopens, and whose new episode is reviewed afresh and quarantined in its first round for missing the goal;
- reviewers that always revise, stopped after round 3 with the revision budget spent.
"""

from __future__ import annotations

from runner.scripted import TREE
from smoke import plan
from smoke import test_phase_lane as lanes
from smoke.test_phase_lane import (
    APPROVE,
    BRANCH,
    INIT,
    INITIATIVE,
    PHASE,
    Worker,
    _answers,
    _approve_tasks,
    _clone_trace,
    _commits,
    _objects_since,
    _owner,
    _reviews,
    _row,
    _run_log,
    _steps,
)

lane_machine = lanes.lane_machine  # the machine these tests request, as a fixture of this module too

HELPER = "def helper(): ..."
REVISE = {"verdict": "revise", "rationale": "a helper is written twice",
          "findings": [{"charter_principle": "fat", "file": "lib/copy.py",
                        "detail": "lib/copy.py repeats lib/helper.py: delete lib/copy.py"}]}
REJECT = {"verdict": "reject", "findings": [],
          "rationale": "t4's note is written but nothing reads it: the phase's goal is missed"}
DELETE_COPY = (f"diff --git a/lib/copy.py b/lib/copy.py\ndeleted file mode 100644\n--- a/lib/copy.py\n+++ /dev/null\n"
               f"@@ -1 +0,0 @@\n-{HELPER}\n")


def _with_file(answers: dict, task: str, path: str, text: str) -> None:
    """Task `task`'s scripted build also adds `path` holding `text`."""
    build = answers[f"build@{task}"]
    build["patch"] += plan.new_file_patch(path, text)
    build["files_touched"].append(path)


def _build(patch: str, files: list[str]) -> dict:
    return {"patch": patch, "summary": "revised", "files_touched": files, "commands_run": []}


def _rounds(sandbox) -> list[tuple]:
    return [tuple(r) for r in _owner(
        sandbox, "SELECT round, verdict, reason, COALESCE(revision_json->>'id', ''), head_sha FROM review_rounds "
                 "WHERE initiative = %s AND phase = %s ORDER BY round", args=(INITIATIVE, PHASE))[0]]


def _phase_steps(sandbox, run_id: str) -> list[str]:
    """The run's phase steps by round, smoke before review (the store's clock is to the second, so two steps of one
    second tie; the lane's log, `_order`, shows the order they ran in)."""
    keyed = [s["step"].split("/") for s in _steps(sandbox, run_id) if s["step"].startswith("phase/")]
    return ["/".join(k) for k in sorted(keyed, key=lambda k: (int(k[2]), k[1] != "smoke"))]


def _order(log: str, *lines: str) -> list[int]:
    """Where each of `lines` first appears in the lane's log, each looked for after the one before."""
    at, out = 0, []
    for line in lines:
        at = log.index(line, at)
        out.append(at)
    return out


def _tasks(sandbox) -> dict[str, tuple]:
    rows = _owner(sandbox, "SELECT task_id, state, merged_commit, extra_json->'quarantine_mark' FROM work_items "
                           "WHERE initiative = %s AND kind = 'task' ORDER BY task_id", args=(INITIATIVE,))[0]
    return {r[0]: tuple(r[1:]) for r in rows}


def test_a_duplicated_helper_is_revised_away_and_a_reopened_phase_is_reviewed_afresh_and_quarantined(lane_machine):
    sandbox, repo, name, env, init_sha, launchable = lane_machine
    held_before = _objects_since(sandbox, repo.origin, [])
    answers = _answers({"t1": 1, "t2": 1, "t3": 1}, review=None)
    _with_file(answers, "t1", "lib/helper.py", HELPER)
    _with_file(answers, "t2", "lib/copy.py", HELPER)  # built in parallel with t1, it writes the same helper again
    answers[f"build@{PHASE}-rev-1"] = _build(DELETE_COPY, ["lib/copy.py"])
    answers[f"review_phase@{PHASE}-review-1"] = REVISE
    answers[f"review_phase@{PHASE}-review-2"] = APPROVE
    launchable.scripted(answers)
    out = Worker(sandbox, env, "once", "--name", name).finished(timeout=420)

    row = _row(sandbox)
    run_id = row["run_id"]
    assert "finished approved: the phase review approved it in round 2" in out, out
    assert (row["state"], row["outcome"], row["claim_epoch"], row["attempts"]) == ("finished", "approved", 1, 1), row
    commits = _commits(sandbox, repo.origin)
    # One revision commit, after the three tasks, carrying its round; the branch holds one copy of the helper.
    assert sorted(c["task"] for c in commits[:3]) == ["t1", "t2", "t3"] and {c["kind"] for c in commits[:3]} == {"task"}
    assert [(c["task"], c["kind"], c["run"]) for c in commits[3:]] == [(f"{PHASE}-rev-1", "revision", run_id)], commits
    files = sandbox.git(repo.origin, "ls-tree", "-r", "--name-only", BRANCH).split()
    assert "lib/helper.py" in files and "lib/copy.py" not in files, files
    assert sandbox.git(repo.origin, "log", "-1", "--format=%(trailers:key=Cox-Round,valueonly)", BRANCH).strip() == "1"
    # Round 1 at the tasks' tip asked for one revision task; round 2 at the revision's tip approved.
    assert [r[:2] + (r[3], r[4]) for r in _rounds(sandbox)] == [
        (1, "revise", f"{PHASE}-rev-1", commits[2]["sha"]), (2, "approve", "", commits[3]["sha"])]
    # The order inside the lane: smoke, review, the revision built and rolled up, smoke again (the round seam), review.
    assert _phase_steps(sandbox, run_id) == ["phase/smoke/1", "phase/review/1", "phase/smoke/2", "phase/review/2"]
    steps = {(s["task_id"], s["kind"]): s for s in _steps(sandbox, run_id)}
    assert steps[(f"{PHASE}-rev-1", "rollup")]["subject_sha"] == commits[3]["sha"]
    log = _run_log(sandbox, run_id)
    tips = [commits[2]["sha"][:12], commits[3]["sha"][:12]]
    assert _order(log, f"lane: the phase smoke at {tips[0]}", "lane: review round 1", f"lane: building {PHASE}-rev-1",
                  f"lane: {PHASE}-rev-1 rolled up", f"lane: the phase smoke at {tips[1]}", "lane: review round 2")
    # Once per phase: one phase reviewer call a round, none for a task.
    assert _reviews(sandbox, run_id) == ["phase/review/1", "phase/review/2"]
    by_task = {c["task"]: c["sha"] for c in commits}
    assert _approve_tasks(sandbox) == [[t, "approved", by_task[t]] for t in ("t1", "t2", "t3")]

    # Work comes back to the approved phase: t4. The chair reopens it, which ends the review episode; the next lane
    # builds t4 alone and reviews the phase afresh (round 4, after the boundary), never finishing on round 2's approve.
    sandbox.write(f"work/{INITIATIVE}/{PHASE}/t4.md", plan.work_item_text(
        {"id": "t4", "title": "Add note t4", "state": "ready", "phase": PHASE, "repo": "app"}, "Add notes/t4.txt."))
    filled = sandbox.cox("route", "store-fill")
    assert filled.returncode == 0, filled.stdout + filled.stderr
    tick = sandbox.cox("chair", "run", "--once", timeout=180)
    assert tick.returncode == 0, tick.stdout + tick.stderr
    assert (_row(sandbox)["state"], _rounds(sandbox)[-1][:2]) == ("ready", (3, "reopened")), (tick.stdout, _rounds(sandbox))
    second = _answers({"t4": 1}, review=None)
    second[f"review_phase@{PHASE}-review-4"] = REJECT
    launchable.scripted(second)
    whole_before = _objects_since(sandbox, repo.origin, [])
    again = Worker(sandbox, env, "once", "--name", name).finished(timeout=300)

    row = _row(sandbox)
    reason = f"the phase review quarantined it in round 4: {REJECT['rationale']}"
    assert f"finished quarantined: {reason}" in again, again
    assert (row["state"], row["outcome"], row["claim_epoch"], row["reason"]) == ("finished", "quarantined", 2, reason), row
    assert [r[:2] for r in _rounds(sandbox)] == [(1, "revise"), (2, "approve"), (3, "reopened"), (4, "quarantine")]
    assert [s["task_id"] for s in _steps(sandbox, row["run_id"]) if s["kind"] == "build"] == ["t4"]
    assert _phase_steps(sandbox, row["run_id"]) == ["phase/smoke/4", "phase/review/4"]
    assert _reviews(sandbox, row["run_id"]) == ["phase/review/4"]
    # The second lane on this machine fetched only what origin gained since the first one's clone.
    warm = _clone_trace(sandbox, row["run_id"])
    assert (warm["mirror"], warm["how"], warm["received"]) == ("refreshed", "borrowed from the mirror", "0"), warm
    assert int(warm["fetched"]) == whole_before - held_before, (warm, whole_before, held_before)
    # The chair quarantines the open task with the round's reason, which triage reads; the approved ones stay.
    tick = sandbox.cox("chair", "run", "--once", timeout=180)
    assert tick.returncode == 0, tick.stdout + tick.stderr
    tasks = _tasks(sandbox)
    assert [tasks[t][0] for t in ("t1", "t2", "t3", "t4")] == ["approved", "approved", "approved", "quarantined"], tasks
    assert tasks["t4"][2] == {"cause": "review", "reason": reason, "run": row["run_id"]}, tasks
    assert _row(sandbox)["state"] == "finished"  # not reopened while its task waited for the quarantine
    assert sandbox.git(repo.origin, "rev-parse", INIT) == init_sha


def test_a_revision_of_a_file_only_the_phase_holds_is_built_on_the_phase_tip_with_the_lanes_own_clone(lane_machine):
    """E1, 2026-11-21: the lane rolls up in its own clone (E2) while its tasks build in the shared checkout, whose HEAD
    is main; a builder's scratch is cut at its runner's tree, which the lane left at that checkout, so the revision of
    a file the phase itself added did not apply there (`patch_outside_scratch`, three attempts). The scripted runner
    here builds in its tree (`runner.scripted.TREE`), as the Claude Code runner does: the revision deletes
    `lib/copy.py`, which only the phase tip holds, so it builds only on a tree at that tip."""
    sandbox, repo, name, env, _, launchable = lane_machine
    answers = _answers({"t1": 1, "t2": 1, "t3": 1}, review=None)
    _with_file(answers, "t1", "lib/helper.py", HELPER)
    _with_file(answers, "t2", "lib/copy.py", HELPER)
    answers[f"build@{PHASE}-rev-1"] = _build(DELETE_COPY, ["lib/copy.py"])
    answers[f"review_phase@{PHASE}-review-1"] = REVISE
    answers[f"review_phase@{PHASE}-review-2"] = APPROVE
    launchable.scripted({**answers, TREE: True})
    out = Worker(sandbox, env, "once", "--name", name).finished(timeout=420)

    row = _row(sandbox)
    run_id = row["run_id"]
    assert "finished approved: the phase review approved it in round 2" in out, (out, _run_log(sandbox, run_id))
    assert (row["state"], row["outcome"], row["attempts"]) == ("finished", "approved", 1), row
    assert _clone_trace(sandbox, run_id)["how"] == "borrowed from the mirror"  # the lane rolled up in its own clone
    assert "lib/copy.py" not in sandbox.git(repo.path, "ls-tree", "-r", "--name-only", "HEAD").split()  # main has none
    commits = _commits(sandbox, repo.origin)
    assert [(c["task"], c["kind"], c["run"]) for c in commits[3:]] == [(f"{PHASE}-rev-1", "revision", run_id)], commits
    assert sandbox.git(repo.origin, "log", "-1", "--format=%(trailers:key=Cox-Round,valueonly)", BRANCH).strip() == "1"
    files = sandbox.git(repo.origin, "ls-tree", "-r", "--name-only", BRANCH).split()
    assert "lib/helper.py" in files and "lib/copy.py" not in files, files
    assert [r[:2] + (r[3],) for r in _rounds(sandbox)] == [(1, "revise", f"{PHASE}-rev-1"), (2, "approve", "")]
    steps = {(s["task_id"], s["kind"]): s for s in _steps(sandbox, run_id)}
    assert steps[(f"{PHASE}-rev-1", "build")]["ok"] == 1 and steps[(f"{PHASE}-rev-1", "rollup")]["subject_sha"] == (
        commits[3]["sha"])
    # Every builder's tree is gone with the run: the shared checkout lists only itself.
    assert sandbox.git(repo.path, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_reviewers_that_always_revise_stop_after_round_three_with_the_revision_budget_spent(lane_machine):
    sandbox, repo, name, env, init_sha, launchable = lane_machine
    answers = _answers({"t1": 1, "t2": 1, "t3": 1}, review=REVISE)  # every round: revise
    for n in (1, 2):
        answers[f"build@{PHASE}-rev-{n}"] = _build(plan.new_file_patch(f"notes/rev-{n}.txt", f"rev {n}"), [f"notes/rev-{n}.txt"])
    launchable.scripted(answers)
    out = Worker(sandbox, env, "once", "--name", name).finished(timeout=420)

    row = _row(sandbox)
    reason = f"the phase review quarantined it in round 3: revision budget spent: {REVISE['rationale']}"
    assert f"finished quarantined: {reason}" in out, out
    assert (row["state"], row["outcome"], row["attempts"], row["reason"]) == ("finished", "quarantined", 1, reason), row
    assert [r[:2] for r in _rounds(sandbox)] == [(1, "revise"), (2, "revise"), (3, "quarantine")]
    commits = _commits(sandbox, repo.origin)
    assert [(c["task"], c["kind"]) for c in commits[3:]] == [(f"{PHASE}-rev-1", "revision"), (f"{PHASE}-rev-2", "revision")]
    assert _phase_steps(sandbox, row["run_id"]) == [f"phase/{k}/{n}" for n in (1, 2, 3) for k in ("smoke", "review")]
    assert _reviews(sandbox, row["run_id"]) == [f"phase/review/{n}" for n in (1, 2, 3)]
    assert not [s for s in _steps(sandbox, row["run_id"]) if s["task_id"] == f"{PHASE}-rev-3"]  # no third revision
    tick = sandbox.cox("chair", "run", "--once", timeout=180)
    assert tick.returncode == 0, tick.stdout + tick.stderr
    tasks = _tasks(sandbox)
    assert {t: v[0] for t, v in tasks.items()} == {"t1": "quarantined", "t2": "quarantined", "t3": "quarantined"}, tasks
    assert {v[2]["reason"] for v in tasks.values()} == {reason}
    assert sandbox.git(repo.origin, "rev-parse", INIT) == init_sha
