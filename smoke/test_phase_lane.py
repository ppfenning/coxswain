"""L1, the phase lane, end to end: a worker claims a phase and its lane builds the tasks in parallel, each rolled up onto
the phase branch on origin as one commit with its `Cox-*` trailers, pushed the moment it lands. Real Postgres, a local
bare origin, the real `cox worker` and `cox chair`; the scripted runner stands in for the model.

The phase has three tasks: t1 and t2 independent, t3 needing both. Each build adds `notes/<task>.txt` and a file under
`waits/` holding the seconds the task's cheap check sleeps, so the test decides how long each task takes: t1 is quick,
t2 slower, so t1's roll-up is seen before t3 can start. The scripted answers carry no review role at all: a task-level
review call would fail the run. `init/ship` is pushed to origin first, as the lander would have it, and must not move.

The lane writes as the worker's scoped store role (`cox setup store worker-role`), named for this test since roles are
server-wide, and dropped at the end. Where the tests stand in for L6, the phase review loop that does not exist yet,
they write its round row as the store's owner ("the scripted round"), and the chair queues the parked phase again.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from smoke import plan
from smoke.conftest import TEAM

psycopg = pytest.importorskip("psycopg")

INITIATIVE, PHASE, BRANCH, INIT = "ship", "p1", "epic/ship/p1", "init/ship"
LEASE_S = 60  # harness.phase_pick.LEASE_S
ROLE_ENV, PASSWORD_ENV = "COX_WORKER_STORE_ROLE", "COX_WORKER_STORE_PASSWORD"
# The task's cheap check: it marks the task running, then sleeps the seconds its `waits/<task>` file holds, or, when
# that file holds `await:<other>`, waits (at most 120 s, then fails) until the other task's check has marked it
# running. A barrier, not a clock: a task that awaits another rolls up only once both are running at the same time.
WAIT_CMD = (
    'for f in $(git diff --name-only "$COX_LANE_BASE" HEAD -- waits); do t=$(basename "$f"); v=$(cat "$f"); '
    'touch "{marks}/running-$t"; case "$v" in await:*) o=${{v#await:}}; i=0; '
    'while [ ! -e "{marks}/running-$o" ] && [ $i -lt 240 ]; do sleep 0.5; i=$((i+1)); done; '
    '[ -e "{marks}/running-$o" ] || exit 1;; *) sleep "$v";; esac; done'
)
THREADS = 6  # the test worker's hosts.threads: at least two task threads per lane at the default three lanes
REVIEW_ROLES = ("review_charter", "review_adversary", "review_arbitrate", "validate_chunk", "validate_phase")


def _patch(task: str, wait_s: int | str) -> str:
    return plan.new_file_patch(f"notes/{task}.txt", task) + plan.new_file_patch(f"waits/{task}", str(wait_s))


def _answers(waits: dict[str, int | str]) -> dict[str, object]:
    """Plan and build only, each task its own build (`build@<task>`): no reviewer is scripted."""
    out: dict[str, object] = {"plan": {"steps": ["write the file"], "files_expected": [], "out_of_scope": []}}
    for task, wait_s in waits.items():
        files = [f"notes/{task}.txt", f"waits/{task}"]
        out[f"build@{task}"] = {"patch": _patch(task, wait_s), "summary": "built", "files_touched": files,
                                "commands_run": []}
    return out


def _owner(sandbox, *sql: str, args: tuple = ()) -> list:
    with psycopg.connect(sandbox.store_url, autocommit=True, connect_timeout=10) as conn:
        out = []
        for statement in sql:
            cur = conn.execute(statement, args or None)
            out.append([list(r) for r in cur.fetchall()] if cur.description else cur.rowcount)
        return out


def _steps(sandbox, run_id: str | None = None) -> list[dict]:
    where, args = ("WHERE run_id = %s", (run_id,)) if run_id else ("", ())
    rows = _owner(sandbox, "SELECT run_id, step, task_id, kind, ok, subject_sha, claim_epoch, started_at, finished_at "
                           f"FROM step_results {where} ORDER BY finished_at, step", args=args)[0]
    keys = ("run_id", "step", "task_id", "kind", "ok", "subject_sha", "claim_epoch", "started_at", "finished_at")
    return [dict(zip(keys, r, strict=True)) for r in rows]


def _row(sandbox) -> dict:
    keys = ("state", "outcome", "reason", "claimed_by", "claim_epoch", "run_id", "attempts", "lease_until")
    found = _owner(sandbox, f"SELECT {', '.join(keys)} FROM phase_queue WHERE initiative = %s AND phase = %s",
                   args=(INITIATIVE, PHASE))[0]
    return dict(zip(keys, found[0], strict=True)) if found else {}


def _store_now(sandbox) -> str:
    return _owner(sandbox, "SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')")[0][0][0]


def _until(check, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(0.5)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def _commits(sandbox, origin: Path) -> list[dict[str, str]]:
    """The phase branch's commits on origin since `init/ship`, oldest first, with their trailers."""
    keys_of = ("Cox-Initiative", "Cox-Phase", "Cox-Task", "Cox-Run", "Cox-Kind")
    fmt = "%H" + "".join(f"%x1f%(trailers:key={k},valueonly,separator=)" for k in keys_of) + "%x1f%P%x1e"
    text = sandbox.git(origin, "log", "--reverse", f"--format={fmt}", f"{INIT}..{BRANCH}")
    keys = ("sha", "initiative", "phase", "task", "run", "kind", "parents")
    return [dict(zip(keys, (v.strip() for v in r.strip().split("\x1f")), strict=True)) for r in text.split("\x1e") if r.strip()]


class Worker:
    """A `cox worker` process with a log file of its own, in the environment that carries its scoped store role."""

    def __init__(self, sandbox, env: dict[str, str], *args: str) -> None:
        self.log = sandbox.home / f"worker-{len(list(sandbox.home.glob('worker-*.log')))}.log"
        with open(self.log, "w", encoding="utf-8") as out:
            self.proc = subprocess.Popen([str(sandbox.install.cox), "worker", *args], env=env, cwd=sandbox.workspace,
                                         text=True, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)

    def said(self) -> str:
        return self.log.read_text(encoding="utf-8")

    def finished(self, timeout: float = 300) -> str:
        assert self.proc.wait(timeout=timeout) == 0, self.said()
        return self.said()

    def kill(self) -> None:
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait(timeout=30)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return True


def _lane_pid(sandbox, run_id: str) -> int:
    return int((sandbox.workspace / "runs" / f"{run_id}.pid").read_text(encoding="utf-8").strip())


def _run_log(sandbox, run_id: str) -> str:
    path = sandbox.workspace / "runs" / f"{run_id}.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


@pytest.fixture
def lane_machine(launchable, repo) -> Iterator[tuple]:
    """The launchable sandbox as one phase-model machine: the phase cartridge, the three tasks filed and queued by one
    chair tick, `init/ship` on origin, and a scoped store role for this test's worker, dropped at the end. Yields
    (sandbox, repo, worker name, the worker's environment, the sha `init/ship` started at)."""
    sandbox = launchable.sandbox
    marks = sandbox.home / "marks"
    marks.mkdir()
    path = launchable.cartridges / TEAM / "cartridge.yaml"
    cartridge = json.loads(path.read_text(encoding="utf-8"))
    cartridge["policy"] = {"dispatch": {"lane_model": "phase", "lane_prereqs": False}}
    cartridge["landing_areas"]["checks"] = [{"name": "lane-probe", "cmd": f"command -v {plan.LANE_PROBE}"},
                                            {"name": "wait", "cmd": WAIT_CMD.format(marks=marks)}]
    path.write_text(json.dumps(cartridge, indent=2) + "\n", encoding="utf-8")
    sandbox.write(f"work/{INITIATIVE}/initiative.md",
                  plan.work_item_text({"id": INITIATIVE, "title": "Ship", "repo": "~/src/app"}, "Ship three notes."))
    for task, needs in (("t1", ""), ("t2", ""), ("t3", "[t1, t2]")):
        fields = {"id": task, "title": f"Add note {task}", "state": "ready", "phase": PHASE, "repo": "app",
                  **({"needs": needs} if needs else {})}
        sandbox.write(f"work/{INITIATIVE}/{PHASE}/{task}.md", plan.work_item_text(fields, f"Add notes/{task}.txt."))
    filled = sandbox.cox("route", "store-fill")
    assert filled.returncode == 0, filled.stdout + filled.stderr
    sandbox.git(repo.path, "push", "--quiet", "origin", f"main:refs/heads/{INIT}")
    init_sha = sandbox.git(repo.origin, "rev-parse", INIT)
    launchable.scripted(_answers({"t1": "await:t2", "t2": 8, "t3": 1}))
    tick = sandbox.cox("chair", "run", "--once", timeout=180)
    assert tick.returncode == 0 and "launch_epic" not in tick.stdout, tick.stdout + tick.stderr
    assert _row(sandbox)["state"] == "ready", tick.stdout

    name = f"smoke-{urlsplit(sandbox.store_url).path.rsplit('_', 1)[-1]}"
    login = "cox_w_" + name.replace("-", "_")
    # The operator's thread budget for this worker's machine, so the lane's share of it does not hang on the cores
    # of whatever runs the test (a 4-core runner at three lanes gets one thread, and builds the tasks one by one).
    _owner(sandbox, "INSERT INTO hosts (name, ssh, capacity, state, beat_at, versions_json, updated_at, updated_by, "
                    "threads) VALUES (%s, '', 1, 'up', '', '{}', '', 'smoke', %s)", args=(name, THREADS))
    try:
        made = sandbox.cox("setup", "store", "worker-role", name)
        assert made.returncode == 0, made.stdout + made.stderr
        lines = (sandbox.home / ".config" / "agent-tools" / "garage.env").read_text(encoding="utf-8").splitlines()
        values = dict(line.split("=", 1) for line in lines if "=" in line)
        env = {**sandbox.env, ROLE_ENV: values[ROLE_ENV], PASSWORD_ENV: values[PASSWORD_ENV]}
        yield sandbox, repo, name, env, init_sha, launchable
    finally:
        _owner(sandbox, f"DO $$BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{login}') "
                        f"THEN EXECUTE 'DROP OWNED BY {login}'; EXECUTE 'DROP ROLE {login}'; END IF; END$$")


def _approve_round(sandbox, head: str) -> None:
    """The scripted round, as L6 will write it: the phase review approved. The chair's next tick queues the parked
    phase again."""
    _owner(sandbox, "INSERT INTO review_rounds VALUES (%s, %s, 1, %s, 'approve', '', '{}', '', 'smoke', 'smoke', 0, 0, '')",
           args=(INITIATIVE, PHASE, head))
    tick = sandbox.cox("chair", "run", "--once", timeout=180)
    assert tick.returncode == 0, tick.stdout + tick.stderr
    assert _row(sandbox)["state"] == "ready", (tick.stdout, _row(sandbox))


_CLONE_TRACE = re.compile(r"lane: clone (?P<url>\S+): mirror (?P<mirror>\w+), mirror fetched (?P<fetched>\d+) object\(s\); "
                          r"(?P<how>borrowed from the mirror|plain clone), received (?P<received>\d+) object\(s\)")


def _clone_trace(sandbox, run_id: str) -> dict[str, str]:
    """What the lane's own clone cost, from the trace line it logs (`harness.repo_cache.trace_line`)."""
    found = _CLONE_TRACE.search(_run_log(sandbox, run_id))
    assert found, _run_log(sandbox, run_id)
    return found.groupdict()


def _objects_since(sandbox, origin: Path, tips: list[str]) -> int:
    """How many objects origin holds that none of `tips` reaches: what a mirror holding `tips` still lacks."""
    listed = sandbox.git(origin, "rev-list", "--objects", "--all", "--not", *tips)
    return len([line for line in listed.splitlines() if line.strip()])


def _lane_refs(sandbox, repo) -> list[str]:
    return sandbox.git(repo.path, "for-each-ref", "--format=%(refname)", "refs/coxswain/").split()


def _freeze_lane_and_lose_its_worker(sandbox, worker: Worker) -> tuple[str, int]:
    """Kill the worker (nobody renews the claim) and freeze its lane's process group where it stands."""
    run_id = _row(sandbox)["run_id"]
    lane = _lane_pid(sandbox, run_id)
    os.killpg(lane, signal.SIGSTOP)
    worker.kill()
    return run_id, lane


def _thaw(lane: int) -> None:
    with __import__("contextlib").suppress(ProcessLookupError):
        os.killpg(lane, signal.SIGCONT)


def test_three_tasks_build_in_parallel_each_pushed_as_it_lands_and_the_approved_round_approves_them(lane_machine):
    sandbox, repo, name, env, init_sha, launchable = lane_machine
    tips_before = sandbox.git(repo.origin, "for-each-ref", "--format=%(objectname)").split()
    held_before = _objects_since(sandbox, repo.origin, [])
    worker = Worker(sandbox, env, "once", "--name", name)
    try:
        first = _until(lambda: [s for s in _steps(sandbox) if s["kind"] == "rollup"], 240, "the first roll-up")[0]
        # Pushed as it lands: origin holds the commit the row names, and t3 has not started.
        on_origin = sandbox.git(repo.origin, "rev-parse", BRANCH)
        assert first["task_id"] == "t1" and first["subject_sha"] == on_origin, first
        assert not [s for s in _steps(sandbox) if s["task_id"] == "t3"], _steps(sandbox)
        # The lane builds in a clone of its own, whose origin is the real one (the mirror is never a remote), and
        # which keeps none of the mirror's objects borrowed.
        own = launchable.worktrees / _row(sandbox)["run_id"] / "_repo"
        assert sandbox.git(own, "remote") == "origin"
        assert sandbox.git(own, "remote", "get-url", "origin") == str(repo.origin)
        assert not (own / ".git" / "objects" / "info" / "alternates").exists()
        # Only the roll-ups happen in the clone (its one extra worktree is the phase's); the tasks build in the shared
        # checkout as before, where t2, still checking, holds its worktree.
        listed = sandbox.git(own, "worktree", "list", "--porcelain").splitlines()
        in_clone = [line for line in listed if line.startswith("worktree ")]
        assert len(in_clone) == 2 and in_clone[1].endswith("/_phase"), in_clone
        in_shared = sandbox.git(repo.path, "worktree", "list", "--porcelain")
        assert in_shared.count("\nworktree ") >= 1, in_shared
        out = worker.finished(timeout=300)
    finally:
        worker.kill()
    row = _row(sandbox)
    run_id = row["run_id"]
    assert "parked as built" in out, out
    assert (row["state"], row["claim_epoch"], row["attempts"]) == ("built", 1, 0), row  # parked, never relaunched

    commits = _commits(sandbox, repo.origin)
    assert [(c["initiative"], c["phase"], c["task"], c["run"], c["kind"]) for c in commits][2:] == [
        (INITIATIVE, PHASE, "t3", run_id, "task")]
    assert sorted(c["task"] for c in commits) == ["t1", "t2", "t3"] and len(commits) == 3, commits
    assert all(len(c["parents"].split()) == 1 for c in commits)  # linear: no merge commits
    assert sandbox.git(repo.origin, "rev-parse", INIT) == init_sha  # one merge owner: the lane never moves init/ship
    assert _lane_refs(sandbox, repo) == []  # the run's private refs are gone with it
    assert not (launchable.worktrees / run_id).exists()  # and its own clone, which held the refs it fetched
    assert not (sandbox.workspace / "runs" / f"{run_id}.lane-beat").exists()
    cold = _clone_trace(sandbox, run_id)
    assert (cold["mirror"], cold["how"], cold["received"]) == ("created", "borrowed from the mirror", "0"), cold
    assert int(cold["fetched"]) == held_before, cold  # the whole of origin as the lane found it, once

    steps = {(s["task_id"], s["kind"]): s for s in _steps(sandbox, run_id)}
    assert {k for k in steps if k[1] == "rollup"} == {("t1", "rollup"), ("t2", "rollup"), ("t3", "rollup")}
    # L2: the repository names no phase level, so the smoke row says so, passed, at the tip it parked.
    assert (steps[("", "smoke")]["ok"], steps[("", "smoke")]["subject_sha"]) == (1, commits[-1]["sha"]), steps
    assert {steps[(c["task"], "rollup")]["subject_sha"] for c in commits} == {c["sha"] for c in commits}
    # t1 and t2 ran at once: t1's check passes only once t2's check is running (the `await:t2` barrier), so its roll-up
    # proves the overlap; and the lane started t2 before t1 rolled up, in the lane's own order of events.
    log = _run_log(sandbox, run_id)
    assert log.index("lane: building t2") < log.index("lane: t1 rolled up"), log
    # t3 started only after both rolled up.
    assert log.index("lane: t2 rolled up") < log.index("lane: building t3"), log
    assert log.index("lane: t1 rolled up") < log.index("lane: building t3"), log
    roles = _owner(sandbox, "SELECT DISTINCT role FROM node_calls WHERE run_id LIKE %s", args=(f"{run_id}%",))[0]
    assert not {r[0] for r in roles} & set(REVIEW_ROLES), roles
    assert f"the lane writes as cox_w_{name.replace('-', '_')}, scoped" in _run_log(sandbox, run_id)

    # The round is written; the chair queues the phase again; the next claim finishes it approved without building,
    # and the chair approves each task at its roll-up commit, both writes at once.
    _approve_round(sandbox, commits[-1]["sha"])
    again = Worker(sandbox, env, "once", "--name", name).finished()
    assert "finished approved" in again, again
    row = _row(sandbox)
    assert (row["state"], row["outcome"], row["claim_epoch"]) == ("finished", "approved", 2), row
    # The second lane on this machine fetched only what origin gained since the first one's clone: its roll-ups.
    warm = _clone_trace(sandbox, row["run_id"])
    assert (warm["mirror"], warm["how"], warm["received"]) == ("refreshed", "borrowed from the mirror", "0"), warm
    gained, whole = _objects_since(sandbox, repo.origin, tips_before), _objects_since(sandbox, repo.origin, [])
    assert int(warm["fetched"]) == gained == whole - held_before, (warm, gained, whole)  # not the whole of origin again
    assert not (launchable.worktrees / row["run_id"]).exists()
    assert not [s for s in _steps(sandbox, row["run_id"]) if s["kind"] in ("build", "rollup")]
    tick = sandbox.cox("chair", "run", "--once", timeout=180)
    assert tick.returncode == 0, tick.stdout + tick.stderr
    acted = _owner(sandbox, "SELECT status FROM chair_actions WHERE kind = 'approve_phase_tasks'")[0]
    assert acted and {a[0] for a in acted} == {"done"}, acted
    tasks = _owner(sandbox, "SELECT task_id, state, merged_commit FROM work_items WHERE initiative = %s AND kind = 'task' "
                            "ORDER BY task_id", args=(INITIATIVE,))[0]
    by_task = {c["task"]: c["sha"] for c in commits}
    assert tasks == [[t, "approved", by_task[t]] for t in ("t1", "t2", "t3")], tasks
    assert sandbox.git(repo.origin, "rev-parse", INIT) == init_sha


def test_a_frozen_lane_is_stopped_by_the_next_claim_which_builds_only_what_is_missing(lane_machine):
    """The worker dies after two roll-ups and its lane freezes mid-check. Once the lease lapses, the next claim on this
    machine stops the frozen lane (it does not refuse its own launch for as long as that lives), sweeps what it left,
    and builds t3 alone; the frozen lane wrote nothing after it froze."""
    sandbox, repo, name, env, init_sha, launchable = lane_machine
    launchable.scripted(_answers({"t1": 1, "t2": 1, "t3": 600}))  # t3's check outlasts everything below
    first = Worker(sandbox, env, "once", "--name", name)
    try:
        _until(lambda: {s["task_id"] for s in _steps(sandbox) if s["kind"] == "rollup"} == {"t1", "t2"}
               and [s for s in _steps(sandbox) if (s["task_id"], s["kind"]) == ("t3", "build")], 240,
               "two roll-ups and t3's build")
        stale_run, lane = _freeze_lane_and_lose_its_worker(sandbox, first)
    finally:
        first.kill()
    try:
        held = _row(sandbox)
        frozen_at = _store_now(sandbox)
        _until(lambda: _store_now(sandbox) > held["lease_until"], LEASE_S + 30, "the lease to lapse by the store's clock")
        launchable.scripted(_answers({"t1": 1, "t2": 1, "t3": 1}))
        resumed = Worker(sandbox, env, "once", "--name", name).finished()
        assert f"stopping {stale_run} (pid {lane}), a lane of an older claim" in resumed, resumed
        assert not _alive(lane)
    finally:
        _thaw(lane)
    row = _row(sandbox)
    assert (row["claim_epoch"], row["state"]) == (2, "built"), (row, resumed)
    assert f"swept what {stale_run} left behind" in _run_log(sandbox, row["run_id"])
    built = [s["task_id"] for s in _steps(sandbox, row["run_id"]) if s["kind"] == "build"]
    assert built == ["t3"], _steps(sandbox, row["run_id"])
    commits = _commits(sandbox, repo.origin)
    assert [(c["task"], c["run"]) for c in commits][2:] == [("t3", row["run_id"])] and len(commits) == 3, commits
    assert {c["run"] for c in commits[:2]} == {stale_run}
    assert not [s for s in _steps(sandbox, stale_run) if s["finished_at"] > frozen_at], _steps(sandbox, stale_run)
    assert _lane_refs(sandbox, repo) == [] and not (launchable.worktrees / stale_run).exists()
    assert sandbox.git(repo.origin, "rev-parse", INIT) == init_sha


PRE_PUSH = """#!/bin/sh
# Smoke: hold t3's roll-up push for a few seconds, after the lane checked its claim and before git sends the refs.
while read -r _ sha _ _; do
  if git log -1 --format=%B "$sha" | grep -qx 'Cox-Task: t3' && [ ! -e {marker} ]; then touch {marker}; sleep 5; fi
done
exit 0
"""


def test_a_lane_frozen_between_its_claim_check_and_its_push_writes_no_row_and_the_next_claim_adopts_the_push(lane_machine):
    """The window no claim check can close: the lane checked its claim, then froze before git sent the refs. Its
    claim lapses and it wakes with the push done behind its back: it writes no row for it and stops. The next claim
    finds t3's commit on origin, records its roll-up, and builds nothing twice."""
    sandbox, repo, name, env, init_sha, _ = lane_machine
    marker = sandbox.home / "pushing-t3"
    hook = repo.path / ".git" / "hooks" / "pre-push"
    hook.write_text(PRE_PUSH.format(marker=marker), encoding="utf-8")
    hook.chmod(0o755)
    first = Worker(sandbox, env, "once", "--name", name)
    try:
        _until(marker.exists, 240, "t3's push to start")
        stale_run, lane = _freeze_lane_and_lose_its_worker(sandbox, first)
    finally:
        first.kill()
    try:
        held = _row(sandbox)
        _until(lambda: _store_now(sandbox) > held["lease_until"], LEASE_S + 30, "the lease to lapse by the store's clock")
        woke_at = _store_now(sandbox)
        _thaw(lane)
        _until(lambda: not _alive(lane), 60, "the woken lane to stop")
    finally:
        _thaw(lane)
    log = _run_log(sandbox, stale_run)
    assert "lane: stopping:" in log, log
    commits = _commits(sandbox, repo.origin)
    assert [c["task"] for c in commits][2:] == ["t3"] and {c["run"] for c in commits} == {stale_run}, commits
    stale = _steps(sandbox, stale_run)
    assert not [s for s in stale if s["finished_at"] >= woke_at] and ("t3", "rollup") not in {
        (s["task_id"], s["kind"]) for s in stale}, stale

    hook.unlink()
    resumed = Worker(sandbox, env, "once", "--name", name).finished()
    row = _row(sandbox)
    assert (row["claim_epoch"], row["state"]) == (2, "built"), (row, resumed)
    mine = {(s["task_id"], s["kind"]): s for s in _steps(sandbox, row["run_id"])}
    assert ("t3", "build") not in mine and mine[("t3", "rollup")]["subject_sha"] == commits[-1]["sha"], mine
    assert len(_commits(sandbox, repo.origin)) == 3
    assert sandbox.git(repo.origin, "rev-parse", INIT) == init_sha


def test_a_refused_push_is_reconciled_and_the_healthy_holder_keeps_its_claim(lane_machine):
    """While t2 is in its check, another lane's commit of t2 lands on origin (a stale lane, played by the test). The
    holder's push of t2 is refused: it checks its claim (still its own), fetches, adopts the commit already there as
    t2's roll-up, and goes on to t3 on top of it. Nothing loses the claim, and no attempt is spent."""
    sandbox, repo, name, env, init_sha, launchable = lane_machine
    launchable.scripted(_answers({"t1": 1, "t2": 25, "t3": 1}))
    worker = Worker(sandbox, env, "once", "--name", name)
    try:
        _until(lambda: [s for s in _steps(sandbox) if (s["task_id"], s["kind"]) == ("t1", "rollup")], 240, "t1's roll-up")
        other = sandbox.home / "other"
        sandbox.git(repo.path.parent, "clone", "--quiet", "--branch", BRANCH, str(repo.origin), str(other))
        for path, text in ((other / "notes" / "t2.txt", "t2\n"), (other / "waits" / "t2", "25\n")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        sandbox.git(other, "add", "-A")
        sandbox.git(other, "commit", "--quiet", "-m",
                    f"epic {INITIATIVE}.p1-9: t2\n\nAdd note t2\n\nCox-Initiative: {INITIATIVE}\nCox-Phase: {PHASE}\n"
                    f"Cox-Task: t2\nCox-Run: {INITIATIVE}.p1-9\nCox-Kind: task\n")
        sandbox.git(other, "push", "--quiet", "origin", f"HEAD:refs/heads/{BRANCH}")
        theirs = sandbox.git(other, "rev-parse", "HEAD")
        out = worker.finished(timeout=300)
    finally:
        worker.kill()
    row = _row(sandbox)
    assert (row["state"], row["claim_epoch"], row["attempts"]) == ("built", 1, 0), (row, out)
    log = _run_log(sandbox, row["run_id"])
    assert "t2's push of" in log and "came back rejected" in log and "next: adopt" in log, log
    assert "lane: stopping:" not in log, log
    commits = _commits(sandbox, repo.origin)
    assert [(c["task"], c["run"]) for c in commits] == [
        ("t1", row["run_id"]), ("t2", f"{INITIATIVE}.p1-9"), ("t3", row["run_id"])], commits
    steps = {(s["task_id"], s["kind"]): s for s in _steps(sandbox, row["run_id"])}
    assert steps[("t2", "rollup")]["subject_sha"] == theirs, steps
    assert sandbox.git(repo.origin, "rev-parse", INIT) == init_sha


# L2: the phase level of the repository's `.agent-checks`, a wiring check none of the tasks' own checks covers. It
# notes where it ran (`smoke-ran`), leaves a marker in that tree, and fails: no note says it is wired in.
SMOKE_CMD = ('echo "$PWD" >> {marks}/smoke-ran && touch .smoke-ran && '
             "{{ grep -q wired notes/t1.txt notes/t2.txt notes/t3.txt || {{ echo 'wiring broken: no note is wired' >&2; exit 1; }}; }}")


def test_a_phase_whose_tasks_pass_alone_but_whose_wiring_is_broken_fails_its_smoke_before_any_review(lane_machine):
    """Each task passes its own checks, one of which proves the smoke has run nowhere yet and left no marker in the
    task's tree. Once all three have rolled up, the lane runs the phase level once, in the phase worktree at the tip;
    it fails, so the `smoke` row is the last step, no review role is ever called, and the worker does not park the
    phase `built`: it goes back to the queue with the smoke's failure as its reason, one attempt spent."""
    sandbox, repo, name, env, init_sha, launchable = lane_machine
    marks = sandbox.home / "marks"
    (repo.path / ".agent-checks").write_text(f"# level: phase\n{SMOKE_CMD.format(marks=marks)}\n", encoding="utf-8")
    sandbox.git(repo.path, "add", ".agent-checks")
    sandbox.git(repo.path, "commit", "--quiet", "-m", "the phase level")
    path = launchable.cartridges / TEAM / "cartridge.yaml"
    cartridge = json.loads(path.read_text(encoding="utf-8"))
    cartridge["landing_areas"]["checks"].append(
        {"name": "no-smoke-yet", "cmd": f"test ! -e .smoke-ran && test ! -e {marks}/smoke-ran"})
    path.write_text(json.dumps(cartridge, indent=2) + "\n", encoding="utf-8")
    launchable.scripted(_answers({"t1": 1, "t2": 1, "t3": 1}))
    out = Worker(sandbox, env, "once", "--name", name).finished()

    row = _row(sandbox)
    run_id = row["run_id"]
    assert "parked as built" not in out and "released for a retry" in out, out
    assert (row["state"], row["claim_epoch"], row["attempts"]) == ("ready", 1, 1), row
    assert row["reason"].startswith("the phase smoke failed: configured check failed: ") and "wiring broken" in row["reason"], row

    commits = _commits(sandbox, repo.origin)
    assert sorted(c["task"] for c in commits) == ["t1", "t2", "t3"], commits  # every task passed and rolled up
    steps = _steps(sandbox, run_id)
    (smoke,) = [s for s in steps if s["kind"] == "smoke"]
    assert (smoke["ok"], smoke["step"], smoke["subject_sha"]) == (0, "phase/smoke/1", commits[-1]["sha"]), smoke
    # It started once the last roll-up was written (the store's clock, to the second), at the tip that roll-up made.
    assert smoke["started_at"] >= max(s["finished_at"] for s in steps if s["kind"] == "rollup"), steps
    log = _run_log(sandbox, run_id)
    assert max(log.index(f"lane: {t} rolled up") for t in ("t1", "t2", "t3")) < log.index("lane: the phase smoke runs"), log
    # The smoke ran once, in the phase worktree, and in no task's: each task's `no-smoke-yet` check passed.
    ran = (marks / "smoke-ran").read_text(encoding="utf-8").splitlines()
    assert len(ran) == 1 and ran[0].endswith(f"/{run_id}/{PHASE}/_phase"), ran
    assert {s["task_id"] for s in steps if s["kind"] == "checks" and s["ok"]} == {"t1", "t2", "t3"}, steps
    roles = _owner(sandbox, "SELECT DISTINCT role FROM node_calls WHERE run_id LIKE %s", args=(f"{run_id}%",))[0]
    assert not {r[0] for r in roles} & set(REVIEW_ROLES), roles
    assert not _owner(sandbox, "SELECT 1 FROM review_rounds WHERE initiative = %s", args=(INITIATIVE,))[0]
    assert f"lane: the phase smoke at {commits[-1]['sha'][:12]} failed" in _run_log(sandbox, run_id)
    assert sandbox.git(repo.origin, "rev-parse", INIT) == init_sha
