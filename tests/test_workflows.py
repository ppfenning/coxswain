"""The docs workflow builds from this checkout alone: no component repository is cloned or installed.

The CLI reference is generated from the `cox` the checkout installs into its own venv, and docs/_pull.py reads
the in-repo folders from disk (only dash, a separate repository, is fetched at its manifest tag).
"""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "docs.yml"


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
