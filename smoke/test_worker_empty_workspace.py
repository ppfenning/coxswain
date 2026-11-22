"""A worker keeps no state: on a machine whose workspace has no `work/` at all, it claims and runs a phase (build,
smoke and review) and the initiative review, for an initiative that exists, for that worker, only in the store.

The chair is the sandbox's own workspace, as in `test_phase_lane.py` and `test_initiative_review.py`, whose fixtures
these reuse. The worker is given a workspace of its own: a routing profile of its own (`AGENT_TOOLS_PROFILE`) that
names the same store, cartridge and provider profile, the machine-local configuration a worker legitimately has, and
a workspace directory with nothing in it. The suites these reuse share one workspace between the chair and the worker,
so a lane that read the chair's `work/<initiative>/initiative.md` passed there and failed on every other machine
(2026-11-21, E1: `routing: no initiative.md at work/...`, three attempts).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

from smoke.test_initiative_review import (
    INIT,
    _build,
    _claim_once,
    _lander,
    _origin_tip,
    _phase_row,
    _plan_answers,
    _queued_review,
    _rounds,
    _suites,
    _tick,
    review_machine,
)
from smoke.test_phase_lane import (
    APPROVE,
    BRANCH,
    Worker,
    _answers,
    _commits,
    _owner,
    _row,
    _steps,
    lane_machine,
)

__all__ = ["lane_machine", "review_machine"]  # the fixtures, imported from the suites they come from, found by name here

PROFILE_ENV = "AGENT_TOOLS_PROFILE"
ELSEWHERE = "smoke-chair@the-chair-machine:1"  # a chair lease holder on another machine (`chair_read_lease._host`)


def _own_workspace(sandbox, env: dict[str, str]) -> tuple[dict[str, str], Path]:
    """The worker's environment with a routing profile of its own, the chair's but for an empty `workspace_dir`."""
    ws = sandbox.home.parent / "worker-workspace"
    ws.mkdir()
    chair_profile = sandbox.home / ".config" / "agent-tools" / "profile.yaml"
    lines = [f"workspace_dir: {ws}" if line.startswith("workspace_dir:") else line
             for line in chair_profile.read_text(encoding="utf-8").splitlines()]
    own = sandbox.home / "worker-profile.yaml"
    own.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {**env, PROFILE_ENV: str(own)}, ws


@contextlib.contextmanager
def _chair_elsewhere(sandbox) -> Iterator[None]:
    """The chair lease named another machine's, so the worker's launch is on a machine that is not the chair's (the
    sandbox's chair and worker share one hostname): it may not export the board, as no machine but the chair may.
    The chair's own holder is put back after, for its next tick."""
    found = _owner(sandbox, "SELECT holder FROM leases WHERE name = 'chair'")[0]
    if found:
        _owner(sandbox, "UPDATE leases SET holder = %s WHERE name = 'chair'", args=(ELSEWHERE,))
    else:
        _owner(sandbox, "INSERT INTO leases (name, holder, epoch, heartbeat_at, expires_at) "
                        "VALUES ('chair', %s, 1, '', '9999-12-31T00:00:00+00:00')", args=(ELSEWHERE,))
    try:
        yield
    finally:
        if found:
            _owner(sandbox, "UPDATE leases SET holder = %s WHERE name = 'chair'", args=(found[0][0],))
        else:
            _owner(sandbox, "DELETE FROM leases WHERE name = 'chair'")


def _no_work_files(ws: Path) -> None:
    """Nothing the worker ran left the initiative in its workspace, where a later launch would read it."""
    assert not (ws / "work").exists(), sorted(str(p.relative_to(ws)) for p in (ws / "work").rglob("*"))


def test_a_worker_with_an_empty_workspace_builds_smokes_and_reviews_a_phase_known_only_to_the_store(lane_machine):
    sandbox, repo, name, env, init_sha, launchable = lane_machine
    (repo.path / ".agent-checks").write_text("# level: phase\ntrue\n", encoding="utf-8")
    sandbox.git(repo.path, "add", ".agent-checks")
    sandbox.git(repo.path, "commit", "--quiet", "-m", "the phase level")
    worker_env, ws = _own_workspace(sandbox, env)
    launchable.scripted(_answers({"t1": 1, "t2": 1, "t3": 1}))

    with _chair_elsewhere(sandbox):
        out = Worker(sandbox, worker_env, "once", "--name", name).finished()

    row = _row(sandbox)
    assert "finished approved: the phase review approved it in round 1" in out, out
    assert (row["state"], row["outcome"], row["attempts"]) == ("finished", "approved", 1), (row, out)
    assert (ws / "runs" / f"{row['run_id']}.log").exists(), "the lane ran from the worker's own workspace"
    assert not (sandbox.workspace / "runs" / f"{row['run_id']}.log").exists()
    commits = _commits(sandbox, repo.origin)
    assert sorted(c["task"] for c in commits) == ["t1", "t2", "t3"], commits
    steps = {(s["task_id"], s["kind"]): s for s in _steps(sandbox, row["run_id"])}
    assert {k for k in steps if k[1] in ("build", "rollup")} == {(t, k) for t in ("t1", "t2", "t3") for k in ("build", "rollup")}
    assert (steps[("", "smoke")]["ok"], steps[("", "smoke")]["subject_sha"]) == (1, commits[-1]["sha"]), steps
    assert (steps[("", "review")]["step"], steps[("", "review")]["subject_sha"]) == ("phase/review/1", commits[-1]["sha"])
    assert sandbox.git(repo.origin, "rev-parse", BRANCH) == commits[-1]["sha"]
    _no_work_files(ws)


def test_a_worker_with_an_empty_workspace_runs_the_initiative_review_of_an_initiative_known_only_to_the_store(review_machine):
    sandbox, name, env, launchable, clone = review_machine
    worker_env, ws = _own_workspace(sandbox, env)
    both = {"patch": _build("b", "notes/b.txt")["patch"] + _build("fix", "notes/fix.txt")["patch"], "summary": "built",
            "files_touched": ["notes/b.txt", "notes/fix.txt"], "commands_run": []}
    launchable.scripted({**_plan_answers(p1_review_1=APPROVE, p2_review_1=APPROVE, initiative_review_1=APPROVE),
                         "build@b": both})
    for phase in ("p1", "p2"):
        with _chair_elsewhere(sandbox):
            out = _claim_once(sandbox, worker_env, name)
        assert (_phase_row(sandbox, phase)["state"], _phase_row(sandbox, phase)["outcome"]) == ("finished", "approved"), out
    _tick(sandbox)  # the tasks approved at their roll-ups
    out = _lander(sandbox)
    assert all(f"phase {phase} of ship merged into {INIT}" in out for phase in ("p1", "p2")), out
    tip = _origin_tip(sandbox, clone, INIT)
    assert _queued_review(sandbox)["base_ref"] == INIT

    with _chair_elsewhere(sandbox):
        out = _claim_once(sandbox, worker_env, name)

    row = _phase_row(sandbox, "initiative")
    assert f"finished approved: the initiative review approved it in round 1 and the suite passed at {tip[:12]}" in out, out
    assert (row["state"], row["outcome"], row["attempts"]) == ("finished", "approved", 1), (row, out)
    assert (ws / "runs" / f"{row['run_id']}.log").exists(), "the review ran from the worker's own workspace"
    assert _rounds(sandbox) == [(1, "approve", tip)]
    assert _suites(sandbox) == [("phase/suite/1", 1, tip)]
    _no_work_files(ws)
