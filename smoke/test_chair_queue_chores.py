"""C6: with the tick stopped, the housekeeping worker does the tick's loop chores. Against a real Postgres; nothing
is stubbed.

The workspace is a git checkout of a bare remote (`chair.workspace_git` is on by default). Every kind is listed in
`chair.queues` and no tick runs at any point. A second clone of the workspace files an intake as a file and pushes it.
Then the workers run as they do under their units:

- housekeeping, one pass on its own first, pulls the workspace (the intake file arrives) and runs the export, whose
  fill writes the intake's store row (that pass also checks this host's login, which the sandbox lacks, so the host is
  then recorded as logged in);
- the five workers run live;
- the launch worker starts the intake's decompose run, under its own lease, and the run (the scripted runner) writes
  n1's initiative and tasks; the decompose worker is woken by those writes and passes over them.

The chair lease is never taken: no tick did any of it.
"""

from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest

from smoke import plan
from smoke.test_chair_launch_queue import _cartridge, _decompose_ready, _launches, _profile, _queue, _until

psycopg = pytest.importorskip("psycopg")


class Worker:
    """`cox chair worker --kind K`, live, polling often so the test does not wait on the 60 s default."""

    def __init__(self, sandbox, kind: str) -> None:
        self.kind = kind
        self.log = sandbox.home / f"{kind}-worker.log"
        argv = [str(sandbox.install.cox), "chair", "worker", "--kind", kind, "--settle", "0", "--poll", "3",
                "--min-gap", "1", "--label", f"{kind}-worker"]
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
        return f"--- {self.kind} ---\n{self.said()}"


def _git_workspace(sandbox, tmp_path: Path) -> Path:
    """The workspace as a clone of a bare remote, and a second clone of it, as another machine's copy."""
    origin, other = tmp_path / "workspace-origin.git", tmp_path / "laptop"
    sandbox.git(tmp_path, "init", "--quiet", "--bare", str(origin))
    ws = sandbox.workspace
    sandbox.git(ws, "init", "--quiet", "-b", "main")
    (ws / ".gitignore").write_text("runs/\n", encoding="utf-8")
    (ws / "intake").mkdir(exist_ok=True)
    (ws / "intake" / ".keep").write_text("", encoding="utf-8")
    sandbox.git(ws, "add", "-A")
    sandbox.git(ws, "commit", "--quiet", "-m", "workspace")
    sandbox.git(ws, "remote", "add", "origin", str(origin))
    sandbox.git(ws, "push", "--quiet", "-u", "origin", "main")
    sandbox.git(tmp_path, "clone", "--quiet", str(origin), str(other))
    return other


def _chair_lease(url: str) -> tuple | None:
    with psycopg.connect(url, connect_timeout=10, autocommit=True) as conn:
        return conn.execute("SELECT holder, epoch FROM leases WHERE name = 'chair'").fetchone()


def _asks(url: str) -> list[tuple]:
    with psycopg.connect(url, connect_timeout=10, autocommit=True) as conn:
        return conn.execute("SELECT holder, target, status, reason FROM chair_actions WHERE kind = 'needs_chair' ORDER BY ts").fetchall()


def _intake_rows(sandbox, name: str) -> list[dict]:
    return [r for r in sandbox.store_rows() if name in (r.get("task_id"), r.get("initiative"))]


def test_an_intake_pulled_from_the_workspace_remote_is_decomposed_by_the_workers_with_no_tick(launchable, tmp_path):
    sandbox = launchable.sandbox
    _decompose_ready(launchable)
    # The lane host's prerequisites (docker and the smoke images) are not what this test is about, and a docker that
    # does not answer the sandbox's home would refuse the launch for a reason of its own.
    _cartridge(launchable, lane_prereqs=False)
    _queue(_profile(sandbox), "housekeeping, watch, quarantine, decompose, launch")
    laptop = _git_workspace(sandbox, tmp_path)
    (laptop / "intake" / "n1.md").write_text(plan.work_item_text({"title": "Idea n1", "repo": "app"}, "Build n1."), encoding="utf-8")
    sandbox.git(laptop, "add", "-A")
    sandbox.git(laptop, "commit", "--quiet", "-m", "intake n1")
    sandbox.git(laptop, "push", "--quiet", "origin", "main")
    assert not (sandbox.workspace / "intake" / "n1.md").exists()
    assert _intake_rows(sandbox, "n1") == []

    # Housekeeping's first pass, on its own: it pulls the intake file in and its export fills the intake's row.
    first = sandbox.cox("chair", "worker", "--kind", "housekeeping", "--once", "--settle", "0", "--label", "first-pass",
                        timeout=300)
    assert first.returncode == 0 and "export:" in first.stdout, first.stdout + first.stderr
    assert "chores failed" not in first.stdout and "workspace pull failed" not in first.stdout, first.stdout
    assert (sandbox.workspace / "intake" / "n1.md").exists(), first.stdout
    assert _intake_rows(sandbox, "n1"), first.stdout
    # That pass also checked this host's login, which the sandbox does not have (`cox setup token status` finds no
    # token), so the host reads lapsed and would refuse every launch. Record it as logged in: the check is not due again
    # for hours, and the launch is judged on the chores, not on the sandbox's login.
    login = sandbox.cox("host", "login", socket.gethostname(), "true")
    assert login.returncode == 0, login.stdout + login.stderr
    workers = [Worker(sandbox, kind) for kind in ("housekeeping", "decompose", "launch", "watch", "quarantine")]
    try:
        _until(lambda: [r for r in _launches(sandbox.store_url) if r[2] == "done"], 240, "the launch worker's decompose")
        _until(lambda: len([r for r in sandbox.store_rows() if r.get("initiative") == "n1"]) > 1, 240,
               "the decompose run's rows for n1")
        _until(lambda: "| notified:" in workers[1].said(), 60, "a decompose worker pass woken by the store")
        lease = _chair_lease(sandbox.store_url)
        asks = _asks(sandbox.store_url)
    except AssertionError as exc:
        with psycopg.connect(sandbox.store_url, connect_timeout=10, autocommit=True) as conn:
            failed = conn.execute("SELECT status, reason FROM chair_actions WHERE kind = 'launch_decompose' LIMIT 3").fetchall()
        raise AssertionError(f"{exc}\n{failed}\n" + "\n".join(w.stop() for w in workers)) from None
    finally:
        said = "\n".join(w.stop() for w in workers)
    rows = _launches(sandbox.store_url)
    why = f"{said}\n--- launches ---\n{json.dumps(rows, default=str)}\n--- asks ---\n{json.dumps(asks, default=str)}"
    assert len([r for r in rows if r[2] == "done"]) == 1, why
    assert all(r[1].startswith("launch-") for r in rows), why  # made under the launch worker's lease
    assert lease is None, (lease, why)  # no tick ever held the chair
    assert "chores failed" not in said and "workspace pull failed" not in said, why
    assert "launch_decompose" not in workers[1].said(), why  # since C5 the decompose worker launches nothing
    assert "check_login" not in workers[0].said(), why  # not due again: the host was recorded as checked
    # Alone, the first pass rightly said the other four kinds had no worker; once all five ran, no worker said it.
    assert not [a for a in asks if a[1].startswith("chair:") and not a[0].startswith("first-pass@")], why
