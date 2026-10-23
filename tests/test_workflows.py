"""The docs workflow builds from this checkout alone: no component repository is cloned or installed.

The CLI reference is generated from the `cox` the checkout installs into its own venv, and docs/_pull.py reads
the in-repo folders from disk (only dash, a separate repository, is fetched at its manifest tag).
"""

from pathlib import Path

import yaml

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


# The extras job runs the Postgres store tests, which back the test database up with pg_dump and fail under xdist.
_SERIAL_JOBS = {"extras"}


def _missing_xdist(steps: list[tuple[str, str]]) -> list[str]:
    return [f"{job}: {run}" for job, run in steps if job not in _SERIAL_JOBS and "-n auto" not in run]


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
    assert len(steps) == 9
    missing = _missing_xdist(steps)
    assert not missing, "pytest steps without -n auto:\n" + "\n".join(missing)


def test_the_extras_job_runs_each_suite_as_its_own_serial_job():
    """Split per suite so they run side by side; each stays serial for the Postgres store tests (2026-10-23)."""
    job = yaml.safe_load(CI.read_text())["jobs"]["extras"]
    suites = {entry["suite"]: entry["dir"] for entry in job["strategy"]["matrix"]["include"]}
    assert suites == {"root": ".", "tools": "src/tools", "graphs": "src/graphs", "cartridges": "src/cartridges"}
    assert job["strategy"]["fail-fast"] is False
    (pytest,) = [step for step in job["steps"] if step.get("run", "").startswith("uv run pytest")]
    assert "-n auto" not in pytest["run"] and pytest["working-directory"] == "${{ matrix.dir }}"
    assert "postgres" in job["services"]

