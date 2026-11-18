"""`cox lake sync`, `query` and `doctor` against the run store, from an install with the `lake` extra.

`lake_install` is this tree installed with `[postgres,lake]`, the extra `cox lake` tells a person to install. Until
pyarrow joined that extra, the install it named could not sync: pyiceberg imported, and the sync stopped with
"pyarrow is missing". CI's extras job installs every extra at once, so it never showed there.
"""

from __future__ import annotations

import json

RUN = "life-1"


def _ok(sandbox, *args):
    done = sandbox.cox(*args)
    assert done.returncode == 0, f"cox {' '.join(args)}: {done.stdout}{done.stderr}"
    return done


def test_lake_sync_puts_the_stores_run_in_the_lake_and_query_reads_it_back(launchable, repo, lake_sandbox):
    ran = launchable.lifecycle(repo.path, RUN)
    assert ran.returncode == 0, ran.stdout[-2000:] + ran.stderr[-2000:]
    runs = str(lake_sandbox.workspace / "runs")
    _ok(lake_sandbox, "lake", "sync", "--runs-dir", runs, "--json")
    rows = json.loads(_ok(lake_sandbox, "lake", "query", "--runs-dir", runs, "--json",
                          "select run_id, ended_at is not null as ended from runs order by run_id").stdout)
    assert rows == [{"run_id": RUN, "ended": True}], rows
    # A scripted run makes no node call and writes no trace, so those two tables have no snapshot: a warn, exit 0.
    doctor = _ok(lake_sandbox, "lake", "doctor", "--runs-dir", runs).stdout.splitlines()
    assert "ok table runs: snapshots: 1" in doctor and doctor[-1] == "verdict: warn", doctor


def test_lake_without_its_extra_says_how_to_install_it(sandbox):
    doctor = sandbox.cox("lake", "doctor", "--runs-dir", str(sandbox.workspace / "runs"))
    assert doctor.returncode == 1, doctor.stdout + doctor.stderr
    assert "pip install 'coxswain[lake]'" in doctor.stdout
