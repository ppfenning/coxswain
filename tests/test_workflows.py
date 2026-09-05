"""The docs workflow resolves the tools tag before installing it.

A pinned-but-unpushed tag must not redden the build: the install step falls
back to `main` when `git ls-remote` cannot find the tag, mirroring the rule
`docs/_pull.py` applies per-path via `fallback_url`.
"""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "docs.yml"


def _install_step(steps: list[dict]) -> str:
    """Pure: the run block of the step that installs coxswain-tools via uv."""
    for step in steps:
        run = step.get("run", "")
        if "uv tool install" in run and "coxswain-tools@" in run:
            return run
    raise AssertionError("no step installs coxswain-tools")


def _docs_job_steps() -> list[dict]:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    return workflow["jobs"]["docs"]["steps"]


def test_install_step_resolves_the_tag_with_ls_remote_before_installing():
    run = _install_step(_docs_job_steps())
    assert "ls-remote" in run
    assert "refs/tags/" in run


def test_install_step_installs_at_main_by_default_and_installs_that_ref():
    run = _install_step(_docs_job_steps())
    assert "ref=main" in run
    assert 'coxswain-tools@${ref}"' in run


def test_install_step_no_longer_uses_the_bare_shell_fallback():
    run = _install_step(_docs_job_steps())
    assert "${tag:-main}" not in run
