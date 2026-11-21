"""C4, the hand-offs between the tick and the quarantine and decompose workers, against a real Postgres.

- Triage, then relaunch. With quarantine queued, the tick does not launch an initiative whose triage the worker owns:
  a launch before the worker's write would build it without the failed task. The worker triages (the task goes back
  to ready, flagged), and the tick's next pass launches the initiative, with the flagged task among its ready work.
  Then the tick's next pass, live, relaunches it, and the run it starts reads the flag: it triages t1 (the scripted
  triage holds it for a person), records the verdict and takes the flag off.
- A worker's decompose launch beside the tick's live lock. The tick (`cox chair run`, live) holds the loop lock under
  its own label; the launch worker's real `launch_decompose` (the harness's scripted runner, offline) is let past the
  loop guard because the store says the worker holds `chair:launch` at that epoch (C5: every launch is the launch
  kind's, so a decompose worker launches nothing). The same launch naming a lease the store does not hold, or an epoch
  it has moved past, is refused.

Nothing is stubbed.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from smoke import plan

psycopg = pytest.importorskip("psycopg")

ROOT = Path(__file__).resolve().parents[1]


def _queue(profile: Path, kinds: str) -> None:
    profile.write_text(profile.read_text(encoding="utf-8") + f"chair:\n  queues: [{kinds}]\n", encoding="utf-8")


def _profile(sandbox) -> Path:
    return sandbox.home / ".config" / "agent-tools" / "profile.yaml"


def _would(stdout: str) -> list[str]:
    """The actions of a dry-run tick's status line (`would: kind:subject, ...`)."""
    status = stdout.strip().splitlines()[-1]
    part = next((p for p in status.split(" | ") if p.startswith("would:")), "would:")
    return [w.strip() for w in part.removeprefix("would:").split(",") if w.strip()]


def _dry_tick(sandbox) -> list[str]:
    done = sandbox.cox("chair", "run", "--once", "--dry-run", timeout=180)
    assert done.returncode == 0, done.stdout + done.stderr
    return _would(done.stdout)


# -- triage, then relaunch -----------------------------------------------------------------------------------------

QUARANTINE = """import json
from harness import store_api
url = {url!r}
row = next(r for r in store_api.queue_rows(url, "ship") if r["task_id"] == "t1")
attempt = {{"run": "ship-1", "cause": "review", "reason": "smoke: t1's checks failed", "ts": "2026-11-20T10:00:00Z"}}
extra = {{**(row.get("extra") or {{}}), "attempts": [attempt]}}
print(json.dumps(type(store_api.upsert_queue_row(url, {{**row, "state": "quarantined", "extra": extra}}, "2026-11-20T10:00:00Z")).__name__))
"""


def _seed_ship(sandbox, repo: str | None = None) -> None:
    """A started initiative whose run `ship-1` has exited: t0 done, t1 failed in that run (quarantined, its attempt on
    record), t2 ready. `repo` names the initiative's repository, for a test that launches it."""
    fields = {"id": "ship", "title": "Ship", **({"repo": repo} if repo else {})}
    sandbox.write("work/ship/initiative.md", plan.work_item_text(fields, "Ship it."))
    for task, state in (("t0", "done"), ("t1", "ready"), ("t2", "ready")):
        fields = {"state": state, "title": f"Task {task}", "id": task, "phase": "p1", "repo": "app"}
        sandbox.write(f"work/ship/p1/{task}.md", plan.work_item_text(fields, f"Task {task} body"))
    imported = sandbox.cox("route", "import")
    assert imported.returncode == 0, imported.stdout + imported.stderr
    assert sandbox.python_json(QUARANTINE.format(url=sandbox.store_url)) == "Upserted"
    with psycopg.connect(sandbox.store_url, connect_timeout=10, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, launched_at, ended_at, status) "
            "VALUES ('ship-1', '2026-11-20T09:00:00Z', '2026-11-20T10:00:00Z', 'quarantined')"
        )


def _ship_launches(would: list[str]) -> list[str]:
    return [w for w in would if w.split(":", 1)[0] in ("launch_epic", "relaunch", "clear_branches") and w.endswith(":ship")]


def test_the_tick_launches_a_triaged_initiative_only_after_the_quarantine_worker_has_flagged_its_task(sandbox):
    _seed_ship(sandbox)
    alone = _dry_tick(sandbox)
    assert "request_triage:t1" in alone and _ship_launches(alone), alone  # the tick alone: triage, then launch
    _queue(_profile(sandbox), "quarantine")
    held = _dry_tick(sandbox)
    assert "request_triage:t1" not in held and _ship_launches(held) == [], held  # the worker's triage comes first
    worker = sandbox.cox("chair", "worker", "--kind", "quarantine", "--once", "--settle", "0", timeout=180)
    assert worker.returncode == 0, worker.stdout + worker.stderr
    assert "1x request_triage done" in worker.stdout, worker.stdout
    task = sandbox.store_row("ship", "t1")
    assert task["state"] == "ready" and (task.get("extra") or {}).get("triage_requested"), task
    after = _dry_tick(sandbox)
    assert "request_triage:t1" not in after and _ship_launches(after), after  # the next pass launches it, t1 ready


# The scripted triage answer: hold t1 for a person, quoting the attempt's reason and citing it (the graph's own check).
TRIAGE_ANSWER = {"class": "hold", "diagnosis": "smoke: t1's checks failed", "cites": ["attempt-1|reason"], "action": "a person decides"}
TRIAGE_SKILLS = {**plan.SMOKE_SKILLS, "triage": "smoke-skills:triage"}


def _triage_ready(launchable) -> None:
    """The team cartridge with the triage role bound, and the scripted answers of a run that triages t1 and builds t2."""
    cartridge = launchable.cartridges / plan_team(launchable)
    text = json.loads((cartridge / "cartridge.yaml").read_text(encoding="utf-8"))
    (cartridge / "cartridge.yaml").write_text(json.dumps({**text, "skills": TRIAGE_SKILLS}, indent=2) + "\n", encoding="utf-8")
    for relative, body in plan.skill_plugin_files(TRIAGE_SKILLS).items():
        (launchable.skills / relative).parent.mkdir(parents=True, exist_ok=True)
        (launchable.skills / relative).write_text(body, encoding="utf-8")
    note = plan.SMOKE_NOTE  # the file the team's configured check reads
    launchable.scripted({**plan.scripted_epic_responses(plan.new_file_patch(note, "ok"), [note]), "triage": TRIAGE_ANSWER})


def _wait_exit(pid: int, seconds: float = 300) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.2)
    return False


def test_the_ticks_live_relaunch_after_the_workers_triage_runs_with_the_flag_and_triages_the_task(launchable, repo):
    sandbox = launchable.sandbox
    _triage_ready(launchable)
    _seed_ship(sandbox, repo="~/src/app")
    _queue(_profile(sandbox), "quarantine")
    held = _dry_tick(sandbox)
    assert "request_triage:t1" not in held and _ship_launches(held) == [], held
    worker = sandbox.cox("chair", "worker", "--kind", "quarantine", "--once", "--settle", "0", timeout=180)
    assert worker.returncode == 0 and "1x request_triage done" in worker.stdout, worker.stdout + worker.stderr
    flag = (sandbox.store_row("ship", "t1").get("extra") or {}).get("triage_requested")
    assert flag, sandbox.store_row("ship", "t1")
    # The tick's next pass, live: it relaunches the initiative itself, and the run it starts reads the flag.
    tick = sandbox.cox("chair", "run", "--once", timeout=300)
    assert tick.returncode == 0, tick.stdout + tick.stderr
    runs = sandbox.workspace / "runs"
    launched = sorted(p.name.removesuffix(".launched.json") for p in runs.glob("ship-*.launched.json"))
    assert launched == ["ship-2"], tick.stdout + "\n" + str(launched)
    pid = int((runs / "ship-2.pid").read_text(encoding="utf-8"))
    assert _wait_exit(pid), f"the relaunched run (pid {pid}) was still running"
    log = (runs / "ship-2.log").read_text(encoding="utf-8")
    t1 = sandbox.store_row("ship", "t1")
    extra = t1.get("extra") or {}
    why = tick.stdout + "\n--- run ---\n" + log[-4000:] + "\n--- t1 ---\n" + json.dumps(t1, default=str)
    # The run triaged t1 because the chair asked: one review-caused attempt is under the run's own attempt cap (2), so
    # without the flag t1 would have been rebuilt as it was. The flag is off the row, the verdict is on it, and the
    # hold put t1 with a person.
    assert "triage_requested" not in extra, why
    assert [(r.get("class"), r.get("run")) for r in extra.get("triage") or []] == [("hold", "ship-2:p1:t1")], why
    assert t1["state"] == "blocked", why


# -- the launch worker's decompose launch beside the tick's live lock --------------------------------------------------------

DECOMPOSE_SKILLS = {**plan.SMOKE_SKILLS, "decompose": "smoke-skills:decompose", "review_adversary": "smoke-skills:review-adversary"}


def _decompose_ready(launchable) -> None:
    """The team cartridge with the decompose graph's two roles bound, and the scripted runner's answers for it."""
    cartridge = launchable.cartridges / plan_team(launchable)
    text = json.loads((cartridge / "cartridge.yaml").read_text(encoding="utf-8"))
    # item_create through the built-in workstore arm: it writes each proposed item from its `apply` payload, in code.
    arms = {"write_kinds": {"item_create": {"apply_arm": "workstore"}}}
    (cartridge / "cartridge.yaml").write_text(json.dumps({**text, **arms, "skills": DECOMPOSE_SKILLS}, indent=2) + "\n", encoding="utf-8")
    for relative, body in plan.skill_plugin_files(DECOMPOSE_SKILLS).items():
        (launchable.skills / relative).parent.mkdir(parents=True, exist_ok=True)
        (launchable.skills / relative).write_text(body, encoding="utf-8")
    answers = json.loads((ROOT / "src" / "graphs" / "fixtures" / "decompose-run.json").read_text(encoding="utf-8"))
    launchable.scripted(answers)


def plan_team(launchable) -> str:
    return next(p.name for p in launchable.cartridges.iterdir() if p.name != "base")


# A worker that held chair:launch and lost it: another took the lease over, so the store's epoch moved past the
# first one's. Prints the first holder's epoch, now stale.
LOST = """import json
from harness import store_chair_lease
from harness.store_dialect import connect
conn = connect({url!r})
try:
    first = store_chair_lease.acquire(conn, "chair:launch", "launch-a", 60)
    store_chair_lease.release(conn, "chair:launch", "launch-a", first)
    store_chair_lease.acquire(conn, "chair:launch", "launch-b", 60)
finally:
    conn.close()
print(json.dumps(first))
"""


def _decompose_rows(url: str) -> list[tuple]:
    with psycopg.connect(url, connect_timeout=10, autocommit=True) as conn:
        return conn.execute("SELECT epoch, holder, status, reason FROM chair_actions WHERE kind = 'launch_decompose' ORDER BY ts").fetchall()


class Tick:
    """`cox chair run`, live: the loop that holds the chair lock under its own label while it runs."""

    def __init__(self, sandbox) -> None:
        argv = [str(sandbox.install.cox), "chair", "run", "--interval", "600"]
        self.proc = subprocess.Popen(argv, env=sandbox.env, cwd=sandbox.home, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def stop(self) -> str:
        self.proc.terminate()
        try:
            out, _ = self.proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, _ = self.proc.communicate(timeout=30)
        return out


def _chair_session(sandbox, timeout: float = 120) -> str:
    """The label of the live chair lock once the tick has written it."""
    lock = sandbox.workspace / "runs" / "chair.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if lock.is_file():
            session = json.loads(lock.read_text(encoding="utf-8")).get("session")
            if session:
                return str(session)
        time.sleep(0.2)
    raise AssertionError("the tick wrote no chair lock")


def test_a_decompose_launch_from_the_launch_worker_goes_past_the_ticks_live_lock_under_its_held_lease(launchable):
    sandbox = launchable.sandbox
    _decompose_ready(launchable)
    _queue(_profile(sandbox), "decompose, launch")
    text = plan.work_item_text({"title": "A new idea", "repo": "app"}, "Build the new idea.")
    sandbox.write("intake/n1.md", text)  # the idea file the decompose reads, and its row in the store
    imported = sandbox.cox("route", "import")
    assert imported.returncode == 0, imported.stdout + imported.stderr
    tick = Tick(sandbox)
    try:
        session = _chair_session(sandbox)
        decompose = sandbox.cox("chair", "worker", "--kind", "decompose", "--once", "--settle", "0", "--label", "decompose-worker", timeout=600)
        worker = sandbox.cox("chair", "worker", "--kind", "launch", "--once", "--settle", "0", "--label", "launch-worker", timeout=600)
        out = tick.stop() if tick.proc.poll() is None else tick.proc.communicate()[0]
    finally:
        if tick.proc.poll() is None:
            tick.stop()
    assert session != "launch-worker", session
    assert decompose.returncode == 0 and "launch_decompose" not in decompose.stdout, decompose.stdout + decompose.stderr
    rows = _decompose_rows(sandbox.store_url)
    why = worker.stdout + worker.stderr + "\n--- rows ---\n" + json.dumps(rows, default=str) + "\n--- tick ---\n" + out
    assert worker.returncode == 0, why
    assert "1x launch_decompose done" in worker.stdout, why
    assert [(r[0], r[2]) for r in rows] == [(1, "done")], why  # once, under the worker's first epoch
    # The loop guard met the tick's live lock and let the launch past on the worker's lease, not on an idle lock.
    assert f"decompose launch under chair:launch at epoch 1, beside {session}'s live loop" in rows[0][3], why
    tasks = {r["task_id"] for r in sandbox.store_rows() if r["initiative"] == "n1" and r["kind"] == "task"}
    assert tasks, why  # the decompose ran to the end and wrote its plan


def test_a_decompose_launch_naming_a_lease_the_store_does_not_hold_is_refused_beside_the_ticks_lock(launchable):
    sandbox = launchable.sandbox
    _decompose_ready(launchable)
    sandbox.write("intake/n1.md", plan.work_item_text({"title": "A new idea", "repo": "app"}, "Build the new idea."))
    tick = Tick(sandbox)
    try:
        session = _chair_session(sandbox)
        argv = ["route", "launch", "decompose", "--idea", str(sandbox.workspace / "intake" / "n1.md"), "--initiative-id", "n1",
                "--no-claim", "--label", "launch-worker"]
        stale = sandbox.python_json(LOST.format(url=sandbox.store_url))
        # the worker that lost the lease, its stale epoch; the new holder at an epoch the store never gave; nobody
        for holder, epoch in (("launch-a", stale), ("launch-b", stale + 7), ("nobody", 1)):
            refused = sandbox.cox(*argv, "--queue-lease", "chair:launch", "--queue-holder", holder, "--queue-epoch", str(epoch), timeout=180)
            assert refused.returncode == 2, refused.stdout + refused.stderr
            assert f"refusing: the landing loop is held by {session} (live)" in refused.stdout, refused.stdout
            assert f"chair:launch is not held by {holder} at epoch {epoch}" in refused.stdout, refused.stdout
        plain = sandbox.cox(*argv, timeout=180)
        assert plain.returncode == 2 and "refusing: the landing loop is held by" in plain.stdout, plain.stdout
    finally:
        tick.stop()
