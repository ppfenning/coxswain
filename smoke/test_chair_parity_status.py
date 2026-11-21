"""C6, end to end: `cox chair parity` (§5.3 step 3) and `cox chair status` (§5.4 step 5), against a real Postgres.

The C5 parity smoke's seed (something due for housekeeping, quarantine and decompose) goes into the sandbox's store,
which is then copied, as an operator restores a dump into a throwaway store. `cox chair parity --store-url <copy>`
runs the tick and every worker dry against the copy and reports a match; pointed at the profile's own store it
refuses. `cox chair status` with every kind listed names every lease and exits 1 while a listed kind has no worker;
with a real housekeeping worker running it shows that lease live, held by that worker. Nothing is stubbed.
"""

from __future__ import annotations

import subprocess
import threading
import time

import pytest

from smoke import plan
from smoke.test_chair_queue_parity import KINDS, _list, _seed

psycopg = pytest.importorskip("psycopg")


@pytest.fixture
def restored(sandbox, session_databases):
    """A copy of the sandbox's store under a name of the same session, dropped at the end."""
    _seed(sandbox)
    source = sandbox.store_url.rsplit("/", 1)[1].split("?")[0]
    name = f"{source[:58]}_copy"
    session_databases.copy(name, source)
    try:
        yield plan.with_database(sandbox.store_url, name)
    finally:
        session_databases.drop(name)


def test_parity_against_a_restored_copy_matches_kind_by_kind_and_refuses_the_live_store(sandbox, restored):
    done = sandbox.cox("chair", "parity", "--store-url", restored, timeout=900)
    assert done.returncode == 0, done.stdout + done.stderr
    out = done.stdout
    assert "parity: match" in out, out
    for kind in KINDS:
        assert f"worker {kind}: " in out and f"--kind {kind} --once --dry-run" in out, out
    alone = next(line for line in out.splitlines() if line.startswith("tick alone"))
    for planned in ("housekeeping", "request_triage", "launch_decompose"):
        assert planned in alone, out
    live = sandbox.cox("chair", "parity", "--store-url", sandbox.store_url, timeout=120)
    assert live.returncode == 2 and "refusing" in live.stdout, live.stdout + live.stderr


def test_status_names_every_lease_and_says_which_listed_kind_has_no_worker(sandbox):
    _list(sandbox, ", ".join(KINDS))
    none = sandbox.cox("chair", "status")
    assert none.returncode == 1, none.stdout + none.stderr
    assert "chair.queues: housekeeping, watch, quarantine, decompose, launch" in none.stdout
    assert all(f"chair:{kind} (listed): no lease row" in none.stdout for kind in KINDS), none.stdout
    assert "NOT RUNNING: housekeeping, watch, quarantine, decompose, launch" in none.stdout

    argv = [str(sandbox.install.cox), "chair", "worker", "--kind", "housekeeping", "--settle", "0"]
    worker = subprocess.Popen(argv, env=sandbox.env, cwd=sandbox.home, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines: list[str] = []
    threading.Thread(target=lambda: lines.extend(l.rstrip() for l in worker.stdout or []), daemon=True).start()
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not any("holds chair:housekeeping" in l for l in lines):
            time.sleep(0.1)
        assert any("holds chair:housekeeping" in l for l in lines), lines
        some = sandbox.cox("chair", "status")
    finally:
        worker.terminate()
        worker.wait(timeout=30)
    assert some.returncode == 1, some.stdout
    line = next(l for l in some.stdout.splitlines() if "chair:housekeeping (listed)" in l)
    assert ": live, held by housekeeping-" in line and "beat " in line and "expires in " in line, some.stdout
    assert "NOT RUNNING: watch, quarantine, decompose, launch" in some.stdout
