"""`cox worker reopen`, end to end: a new-model phase finished `failed` is terminal (the chair never retries it, and
releasing a hold does not either) until the operator reopens it; the next claim then builds only what the phase
branch still lacks.

The machine is `test_phase_lane.py`'s (`lane_machine`): three tasks t1, t2 and t3 (t3 needing both) of phase p1. A
phase is forced `failed` by writing the row a run-out phase is left with (`finished`, `failed`, three attempts), as
the chair's `exhaust` would.
"""

from __future__ import annotations

from smoke import test_phase_lane as lanes
from smoke.test_phase_lane import (
    BRANCH,
    INIT,
    INITIATIVE,
    PHASE,
    Worker,
    _alive,
    _answers,
    _commits,
    _freeze_lane_and_lose_its_worker,
    _owner,
    _row,
    _steps,
    _thaw,
    _until,
)

lane_machine = lanes.lane_machine  # the machine these tests request, as a fixture of this module too

TARGET = f"{INITIATIVE}/{PHASE}"
RAN_OUT = "3 claims and none finished it; the operator decides"


def _fail(sandbox) -> None:
    """Leave the phase as a phase whose claims ran out: finished failed, three attempts, nobody holding it."""
    changed = _owner(sandbox, "UPDATE phase_queue SET state = 'finished', outcome = 'failed', attempts = 3, "
                              "lease_until = '', reason = %s WHERE initiative = %s AND phase = %s",
                     args=(RAN_OUT, INITIATIVE, PHASE))[0]
    assert changed == 1


def _reopen(sandbox, why: str):
    return sandbox.cox("worker", "reopen", TARGET, "--reason", why, "--by", "smoke-op", cwd=sandbox.workspace)


def test_a_failed_phase_is_reopened_by_the_operator_and_the_next_claim_builds_only_the_missing_task(lane_machine):
    sandbox, repo, name, env, init_sha, launchable = lane_machine
    _fail(sandbox)
    # Releasing a hold leaves the finished phase as it is, and says how to retry it.
    held = sandbox.cox("route", "hold", INITIATIVE, "--reason", "look at the failure")
    assert held.returncode == 0, held.stdout + held.stderr
    released = sandbox.cox("route", "release", INITIATIVE)
    assert released.returncode == 0, released.stdout + released.stderr
    assert (f"release: phase {TARGET} is finished failed; reopen it with `cox worker reopen {TARGET} --reason ...`"
            in released.stdout), released.stdout
    assert (_row(sandbox)["state"], _row(sandbox)["outcome"]) == ("finished", "failed")

    reopened = _reopen(sandbox, "nothing ran yet")
    assert reopened.returncode == 0, reopened.stdout + reopened.stderr
    assert reopened.stdout.startswith(f"worker: {TARGET} was finished failed (epoch 0, 3 attempt(s): {RAN_OUT})"), reopened.stdout
    row = _row(sandbox)
    assert (row["state"], row["outcome"], row["attempts"], row["reason"]) == (
        "ready", "", 0, "reopened by smoke-op: nothing ran yet"), row
    assert _owner(sandbox, "SELECT updated_by FROM phase_queue WHERE initiative = %s AND phase = %s",
                  args=(INITIATIVE, PHASE))[0] == [["smoke-op"]]
    again = _reopen(sandbox, "twice")  # idempotent: a ready row is left as it is
    assert (again.returncode, again.stdout.strip()) == (0, f"worker: {TARGET} is ready already (0 attempt(s)); nothing changed")
    assert _row(sandbox) == row

    # The first claim rolls up t1 and t2, then its worker dies with t3 in its check; the phase is left failed.
    launchable.scripted(_answers({"t1": 1, "t2": 1, "t3": 600}))
    first = Worker(sandbox, env, "once", "--name", name)
    try:
        _until(lambda: {s["task_id"] for s in _steps(sandbox) if s["kind"] == "rollup"} == {"t1", "t2"}
               and [s for s in _steps(sandbox) if (s["task_id"], s["kind"]) == ("t3", "build")], 240,
               "two roll-ups and t3's build")
        refused = _reopen(sandbox, "while it runs")
        assert refused.returncode == 1 and f"worker: {TARGET} is claimed by {name} (epoch 1)" in refused.stdout, refused.stdout
        stale_run, lane = _freeze_lane_and_lose_its_worker(sandbox, first)
    finally:
        first.kill()
    try:
        _fail(sandbox)
        retried = _reopen(sandbox, "t3's check is fixed")
        assert retried.returncode == 0 and "was finished failed (epoch 1, 3 attempt(s)" in retried.stdout, retried.stdout
        assert (_row(sandbox)["state"], _row(sandbox)["attempts"]) == ("ready", 0)
        launchable.scripted(_answers({"t1": 1, "t2": 1, "t3": 1}))
        resumed = Worker(sandbox, env, "once", "--name", name).finished()
        assert not _alive(lane), resumed
    finally:
        _thaw(lane)
    row = _row(sandbox)
    assert (row["state"], row["outcome"], row["claim_epoch"], row["attempts"]) == ("finished", "approved", 2, 1), (row, resumed)
    built = [s["task_id"] for s in _steps(sandbox, row["run_id"]) if s["kind"] == "build"]
    assert built == ["t3"], _steps(sandbox, row["run_id"])  # t1 and t2 were kept on the branch and not built again
    commits = _commits(sandbox, repo.origin)
    assert [(c["task"], c["run"]) for c in commits] == [*((c["task"], stale_run) for c in commits[:2]), ("t3", row["run_id"])]
    assert len(commits) == 3 and sandbox.git(repo.origin, "rev-parse", BRANCH) == commits[-1]["sha"], commits
    assert sandbox.git(repo.origin, "rev-parse", INIT) == init_sha
    done = _reopen(sandbox, "once more")
    assert done.returncode == 1 and "finished approved; there is nothing to retry" in done.stdout, done.stdout
