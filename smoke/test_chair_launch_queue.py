"""C5, the `launch` queue worker and the phase queue's order, against a real Postgres. Nothing is stubbed.

- The order. Four phase initiatives are filed one after another, A, B, D, C. B's intake is a hotfix, and C ships in
  A's release. After each one is filed, `cox chair worker --kind launch --once` (the F5 `queue_phases` step, now the
  launch worker's) queues its phase. The queue reads B (band 0), then A and C together (their release), then D. The
  worker's own claim (`store_phases.claim_next`, the call `cox worker` makes) takes them in that order.
- The last lane. One lane (`max_in_flight: 1`) and two intakes waiting for a decompose. The launch worker holds
  `chair:launch` and launches one decompose. Then each of the others that could start a run tries:
  - a tick (`cox chair run --once`) whose profile does not list launch, so only the live lease stops it;
  - a second launch worker, which stands by;
  - the decompose worker, which since C5 launches nothing.
  Exactly one `launch_decompose` is recorded done, under the first worker's epoch: two launches never race for the
  last lane.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from smoke import plan
from smoke.conftest import TEAM

psycopg = pytest.importorskip("psycopg")

ROOT = Path(__file__).resolve().parents[1]


def _profile(sandbox) -> Path:
    return sandbox.home / ".config" / "agent-tools" / "profile.yaml"


def _queue(profile: Path, kinds: str) -> None:
    profile.write_text(profile.read_text(encoding="utf-8") + f"chair:\n  queues: [{kinds}]\n", encoding="utf-8")


def _cartridge(launchable, **policy: object) -> None:
    path = launchable.cartridges / TEAM / "cartridge.yaml"
    cartridge = json.loads(path.read_text(encoding="utf-8"))
    cartridge["policy"] = {"dispatch": {**cartridge.get("policy", {}).get("dispatch", {}), **policy}}
    path.write_text(json.dumps(cartridge, indent=2) + "\n", encoding="utf-8")


def _launch_pass(sandbox, *extra: str) -> subprocess.CompletedProcess[str]:
    done = sandbox.cox("chair", "worker", "--kind", "launch", "--once", "--settle", "0", *extra, timeout=300)
    assert done.returncode == 0, done.stdout + done.stderr
    return done


# -- the order -------------------------------------------------------------------------------------------------------

RELEASE = "0.0.1"  # below any version this tree carries, so the release gate defers nothing


def _file_phase_initiative(sandbox, name: str, **intake: str) -> None:
    """A phase initiative with one ready task in p1, linked to a done intake carrying `intake` (kind, target_release)."""
    sandbox.write(f"intake/done/{name}.md", plan.work_item_text({"title": name.upper(), "repo": "app", **intake}, name))
    fields = {"id": name, "title": name.upper(), "repo": "~/src/app", "intake": f"intake/{name}.md"}
    sandbox.write(f"work/{name}/initiative.md", plan.work_item_text(fields, f"Ship {name}."))
    task = {"id": "t1", "title": f"{name} t1", "state": "ready", "phase": "p1", "repo": "app"}
    sandbox.write(f"work/{name}/p1/t1.md", plan.work_item_text(task, f"Add {name}."))
    filled = sandbox.cox("route", "store-fill")
    assert filled.returncode == 0, filled.stdout + filled.stderr


CLAIM = """import json
from harness import store_phases
from harness.store_dialect import connect
conn = connect({url!r})
taken = []
try:
    while (claim := store_phases.claim_next(conn, "smoke-worker", ["repo:app"], 1)) is not None:
        taken.append(claim.initiative)
finally:
    conn.close()
print(json.dumps(taken))
"""


def _queued_rows(url: str) -> list[tuple]:
    with psycopg.connect(url, connect_timeout=10, autocommit=True) as conn:
        return conn.execute(
            "SELECT initiative, band, seq, queued_at FROM phase_queue WHERE state = 'ready' ORDER BY band, seq"
        ).fetchall()


def test_the_queue_reads_the_hotfix_first_then_a_releases_work_together_and_a_worker_claims_in_that_order(launchable):
    sandbox = launchable.sandbox
    _cartridge(launchable, lane_model="phase", lane_prereqs=False)
    _queue(_profile(sandbox), "launch")
    for name, intake in (("a", {"target_release": RELEASE}), ("b", {"kind": "hotfix"}), ("d", {}),
                         ("c", {"target_release": RELEASE})):
        _file_phase_initiative(sandbox, name, **intake)
        done = _launch_pass(sandbox)
        assert "queue_phases done" in done.stdout, done.stdout + done.stderr
        time.sleep(1.1)  # the store's clock is read to the second: each filing is queued at its own time
    tick = sandbox.cox("chair", "run", "--once", "--dry-run", timeout=180)  # the tick plans no queue write of its own
    assert tick.returncode == 0 and "queue_phases" not in tick.stdout, tick.stdout + tick.stderr
    rows = _queued_rows(sandbox.store_url)
    why = json.dumps(rows, default=str)
    assert [(r[0], r[1]) for r in rows] == [("b", 0), ("a", 1), ("c", 1), ("d", 1)], why
    queued = {r[0]: r[3] for r in rows}
    assert queued["a"] < queued["d"] < queued["c"], why  # filed A, D, C: C goes ahead of D only for its release
    assert sandbox.python_json(CLAIM.format(url=sandbox.store_url)) == ["b", "a", "c", "d"], why


# -- the last lane ---------------------------------------------------------------------------------------------------

DECOMPOSE_SKILLS = {**plan.SMOKE_SKILLS, "decompose": "smoke-skills:decompose", "review_adversary": "smoke-skills:review-adversary"}


def _decompose_ready(launchable) -> None:
    """The team cartridge with one lane and the decompose graph's roles bound, and the scripted runner's answers."""
    path = launchable.cartridges / TEAM / "cartridge.yaml"
    cartridge = json.loads(path.read_text(encoding="utf-8"))
    arms = {"write_kinds": {"item_create": {"apply_arm": "workstore"}}}
    policy = {"dispatch": {"max_in_flight": 1}}
    path.write_text(json.dumps({**cartridge, **arms, "policy": policy, "skills": DECOMPOSE_SKILLS}, indent=2) + "\n", encoding="utf-8")
    for relative, body in plan.skill_plugin_files(DECOMPOSE_SKILLS).items():
        (launchable.skills / relative).parent.mkdir(parents=True, exist_ok=True)
        (launchable.skills / relative).write_text(body, encoding="utf-8")
    launchable.scripted(json.loads((ROOT / "src" / "graphs" / "fixtures" / "decompose-run.json").read_text(encoding="utf-8")))


def _launches(url: str) -> list[tuple]:
    with psycopg.connect(url, connect_timeout=10, autocommit=True) as conn:
        return conn.execute(
            "SELECT epoch, holder, status FROM chair_actions WHERE kind IN ('launch_decompose', 'launch_epic') ORDER BY ts"
        ).fetchall()


def _lease(url: str) -> tuple | None:
    with psycopg.connect(url, connect_timeout=10, autocommit=True) as conn:
        return conn.execute("SELECT holder, epoch FROM leases WHERE name = 'chair:launch'").fetchone()


class Worker:
    """`cox chair worker --kind launch`, live: one pass at once, then no other for the length of the test."""

    def __init__(self, sandbox) -> None:
        self.log = sandbox.home / "launch-worker.log"
        argv = [str(sandbox.install.cox), "chair", "worker", "--kind", "launch", "--settle", "0", "--poll", "900",
                "--min-gap", "900", "--label", "launch-worker"]
        with open(self.log, "w", encoding="utf-8") as out:
            self.proc = subprocess.Popen(argv, env=sandbox.env, cwd=sandbox.home, stdout=out, stderr=subprocess.STDOUT, text=True)

    def said(self) -> str:
        return self.log.read_text(encoding="utf-8")

    def stop(self) -> str:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=30)
        return self.said()


def _until(check, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if found := check():
            return found
        time.sleep(0.5)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def test_one_free_lane_takes_one_launch_whoever_else_tries_to_start_a_run(launchable):
    sandbox = launchable.sandbox
    _decompose_ready(launchable)
    tick_profile = sandbox.home / "tick-profile.yaml"  # the tick's machine does not list launch: only the lease holds it
    tick_profile.write_text(_profile(sandbox).read_text(encoding="utf-8"), encoding="utf-8")
    _queue(_profile(sandbox), "decompose, launch")
    for name in ("n1", "n2"):
        sandbox.write(f"intake/{name}.md", plan.work_item_text({"title": f"Idea {name}", "repo": "app"}, f"Build {name}."))
    imported = sandbox.cox("route", "import")
    assert imported.returncode == 0, imported.stdout + imported.stderr
    worker = Worker(sandbox)
    try:
        _until(lambda: "launch_decompose done" in worker.said(), 180, "the launch worker's first pass")
        held = _lease(sandbox.store_url)
        assert held is not None and held[0].startswith("launch-"), (held, worker.said())
        tick = replace(sandbox, env={**sandbox.env, "AGENT_TOOLS_PROFILE": str(tick_profile)})
        ticked = tick.cox("chair", "run", "--once", timeout=300)
        second = sandbox.cox("chair", "worker", "--kind", "launch", "--once", "--settle", "0", "--label", "launch-2", timeout=180)
        decompose = sandbox.cox("chair", "worker", "--kind", "decompose", "--once", "--settle", "0", timeout=300)
        rows = _launches(sandbox.store_url)
    finally:
        said = worker.stop()
    why = (f"--- worker ---\n{said}\n--- tick ---\n{ticked.stdout}{ticked.stderr}\n--- second ---\n{second.stdout}"
           f"\n--- decompose ---\n{decompose.stdout}{decompose.stderr}\n--- rows ---\n{json.dumps(rows, default=str)}")
    assert ticked.returncode == 0 and "launch_decompose done" not in ticked.stdout, why
    assert second.returncode == 1 and "standby: another worker holds chair:launch" in second.stdout, why
    assert decompose.returncode == 0 and "launch_decompose" not in decompose.stdout, why
    done = [r for r in rows if r[2] == "done"]
    assert len(done) == 1 and done[0][0] == held[1], why  # one launch, under the worker's epoch
