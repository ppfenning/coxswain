"""The docs workflow builds from this checkout alone: no component repository is cloned or installed.

The CLI reference is generated from the `cox` the checkout installs into its own venv, and docs/_pull.py reads
the in-repo folders from disk (only dash, a separate repository, is fetched at its manifest tag).
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

from smoke import plan

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "docs.yml"
CI = Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml"


def _pytest_steps(workflow: dict) -> list[tuple[str, str]]:
    """(job name, command) for every step whose run starts with `uv run pytest`."""
    return [
        (job_name, step["run"])
        for job_name, job in workflow["jobs"].items()
        for step in job.get("steps", [])
        if step.get("run", "").startswith("uv run pytest")
    ]


# The Postgres store tests back the test database up with pg_dump and fail under xdist, so they run in serial steps:
# the extras job's marked postgres step on a pull request, and its whole-suite step on a push to main.
_EXTRAS = 'uv run pytest -q -rs -n auto -m "extras and not postgres" || { test $? -eq 5 && test "${{ matrix.selects }}" = none; }'
_POSTGRES = 'uv run pytest -q -rs -m postgres || { test $? -eq 5 && test "${{ matrix.selects }}" != both; }'
_WHOLE = "uv run pytest -q -rs"
# The smoke suite installs cox once and opens one Postgres per run, so it runs serial (docs/smoke.md).
_SMOKE = 'uv run pytest -q -rs -m "not forge" smoke'
_SMOKE_FORGE = "uv run pytest -q -rs -m forge smoke"
_SMOKES = (_SMOKE, _SMOKE_FORGE)  # serial: one install, one Postgres and one forge per run


def _missing_xdist(steps: list[tuple[str, str]]) -> list[str]:
    serial = (_POSTGRES, _WHOLE, *_SMOKES)
    return [f"{job}: {run}" for job, run in steps if run not in serial and "-n auto" not in run]


def _runs() -> list[str]:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    return [step.get("run", "") for step in workflow["jobs"]["docs"]["steps"]]


def test_the_docs_build_installs_this_checkout_and_no_component_repository():
    runs = _runs()
    assert "uv sync --group dev" in runs
    assert not any("coxswain-tools" in run or "git clone" in run or "uv tool install" in run for run in runs)


def test_the_pull_runs_before_the_cli_reference_and_the_deploy():
    runs = _runs()
    pull = runs.index("uv run python docs/_pull.py")
    assert runs.index("uv sync --group dev") < pull < runs.index("uv run python docs/_cli.py")


def test_every_ci_pytest_step_runs_in_parallel():
    steps = _pytest_steps(yaml.safe_load(CI.read_text()))
    assert len(steps) == 10  # unit (one step per suite, through the matrix), smoke x2, py315 x4, extras x3
    missing = _missing_xdist(steps)
    assert not missing, "pytest steps without -n auto:\n" + "\n".join(missing)


def test_every_unit_pytest_step_is_named_after_its_suite():
    """The land's rerun rule parses `pytest <suite>` from the failed step's name (land._suite_of)."""
    job = yaml.safe_load(CI.read_text())["jobs"]["unit"]
    (step,) = [step for step in job["steps"] if step.get("run", "").startswith("uv run pytest")]
    assert step["name"] == "pytest ${{ matrix.suite }}" and step["working-directory"] == "${{ matrix.dir }}"
    assert step["env"] == {"COX_EXTRAS_GUARD": "unit"}  # a skip neither marked for extras nor allowed fails the suite
    suites = {entry["suite"]: entry["dir"] for entry in job["strategy"]["matrix"]["include"]}
    assert suites == {"root": ".", "tools": "src/tools", "graphs": "src/graphs", "cartridges": "src/cartridges"}
    assert all(re.fullmatch(r"pytest (root|tools|graphs|cartridges)", f"pytest {suite}") for suite in suites)


def test_the_smoke_jobs_run_against_their_own_postgres_behind_the_required_check():
    """`test` is the one required check and needs both smoke jobs, each required to run rather than skip (H10)."""
    ci = yaml.safe_load(CI.read_text())
    assert {"smoke", "smoke-forge"} <= set(ci["jobs"]["test"]["needs"])
    (smoke,) = [step for step in ci["jobs"]["smoke"]["steps"] if step.get("run") == _SMOKE]
    assert smoke["name"] == "smoke"
    assert smoke["env"]["COX_SMOKE_REQUIRED"] == "1"
    assert "job.services.postgres.ports['5432']" in smoke["env"]["COX_SMOKE_POSTGRES_URL"]
    assert smoke["env"][plan.SHARD_ENV] == "${{ matrix.shard }}"
    # the forge tests, in a job of their own, unsharded, with the same Postgres settings and the same refusal to skip
    (forge,) = [step for step in ci["jobs"]["smoke-forge"]["steps"] if step.get("run") == _SMOKE_FORGE]
    shared = {key: value for key, value in smoke["env"].items() if key != plan.SHARD_ENV}
    assert forge["name"] == "smoke (forge)" and forge["env"] == shared
    # pinned by digest, the same image the suite's own container fixture runs
    for name in ("smoke", "smoke-forge"):
        assert ci["jobs"][name]["services"]["postgres"]["image"] == plan.POSTGRES_IMAGE
    assert "@sha256:" in plan.POSTGRES_IMAGE


def test_the_extras_job_runs_the_marked_tests_on_a_pull_request_and_every_test_on_main():
    """One job per suite, side by side. On a pull request: the `extras` tests (harness/extras_tests.py) under xdist,
    then the `postgres` ones serially for the store tests' pg_dump (2026-10-23). On a push to main: the whole suite,
    serially, since product code branches on whether an extra is present. Every suite keeps a job, so a marked test
    added to any suite runs, and the guard runs in `extras` mode: a test the job selects must not skip."""
    job = yaml.safe_load(CI.read_text())["jobs"]["extras"]
    matrix = {entry["suite"]: entry for entry in job["strategy"]["matrix"]["include"]}
    assert {suite: entry["dir"] for suite, entry in matrix.items()} == {
        "root": ".", "tools": "src/tools", "graphs": "src/graphs", "cartridges": "src/cartridges"
    }
    # an empty selection fails except where none is expected: cartridges' two steps and root's postgres step
    assert {suite: entry["selects"] for suite, entry in matrix.items()} == {
        "root": "extras", "tools": "both", "graphs": "both", "cartridges": "none"
    }
    assert job["strategy"]["fail-fast"] is False and job["env"] == {"COX_EXTRAS_GUARD": "extras"}
    steps = [step for step in job["steps"] if step.get("run", "").startswith("uv run pytest")]
    assert [(step["run"], step["if"]) for step in steps] == [
        (_EXTRAS, "github.event_name != 'push'"),
        (_POSTGRES, "github.event_name != 'push'"),
        (_WHOLE, "github.event_name == 'push'"),
    ]
    assert all(step["working-directory"] == "${{ matrix.dir }}" for step in steps)
    # not `pytest <suite>`: the land's rerun rule attributes a failed suite only from the unit steps
    assert all(not step["name"].startswith("pytest ") for step in steps)
    assert "postgres" in job["services"]



def _collected(markexpr: str, shard: str | None = None) -> list[str]:
    """The smoke test ids a real `pytest --collect-only -m <markexpr> smoke` selects, under COX_SMOKE_SHARD=<shard>."""
    env = {key: value for key, value in os.environ.items() if key != plan.SHARD_ENV}
    if shard is not None:
        env[plan.SHARD_ENV] = shard
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "-m", markexpr, "smoke"],
        cwd=CI.parents[2], env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    return [line for line in proc.stdout.splitlines() if "::" in line]


def test_every_smoke_test_runs_in_exactly_one_of_ci_s_shards():
    """No smoke test can drop out of CI. The matrix names shards 1/n .. n/n, and the real collection under each
    COX_SMOKE_SHARD, with the weights the conftest computes from it, splits the non-forge suite with no test missing
    or doubled and no shard empty. The forge job takes the rest, so every smoke file runs somewhere."""
    ci = yaml.safe_load(CI.read_text())
    job = ci["jobs"]["smoke"]
    shards = [entry["shard"] for entry in job["strategy"]["matrix"]["include"]]
    count = plan.parse_shard(shards[0])[1]
    assert 2 <= count <= 3 and sorted(map(plan.parse_shard, shards)) == [(k, count) for k in range(1, count + 1)]
    assert job["strategy"]["fail-fast"] is False and "smoke" in ci["jobs"]["test"]["needs"]
    whole = _collected("not forge")
    split = [_collected("not forge", shard) for shard in shards]
    assert all(split) and sorted(test for part in split for test in part) == sorted(whole)
    files = {f"smoke/{path.name}" for path in (CI.parents[2] / "smoke").glob("test_*.py")}
    assert {test.split("::")[0] for test in whole + _collected("forge")} == files
    # the bare install runs once, in the first shard
    (bare,) = [step for step in job["steps"] if step.get("name") == "bare install (no extras)"]
    assert bare["if"] == f"matrix.shard == '1/{count}'"
