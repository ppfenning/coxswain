"""The negative control: install-proof must reject a deliberately broken formula."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "install-proof-negative.yml"
FIXTURE = ROOT / "tests" / "devtools" / "broken_cox.rb"
FIXTURE_PATH = "tests/devtools/broken_cox.rb"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def test_proof_job_calls_install_proof_with_the_broken_fixture():
    proof = _workflow()["jobs"]["proof"]
    assert proof["uses"] == "./.github/workflows/install-proof.yml"
    assert proof["with"]["formula_path"] == FIXTURE_PATH
    assert proof["with"]["version"]


def test_workflow_triggers_on_dispatch_and_schedule():
    triggers = _workflow()[True]
    assert "workflow_dispatch" in triggers
    assert triggers["schedule"][0]["cron"]


def test_expect_failure_job_passes_only_when_the_proof_failed():
    job = _workflow()["jobs"]["expect-failure"]
    assert job["needs"] == "proof"
    assert "always()" in job["if"]
    script = "\n".join(step.get("run", "") for step in job["steps"])
    env = {k: v for step in job["steps"] for k, v in step.get("env", {}).items()}
    assert "needs.proof.result" in " ".join(env.values())
    assert '= "failure"' in script
    assert "success" not in script


def test_workflow_writes_nothing_and_touches_no_tap():
    text = WORKFLOW.read_text()
    assert _workflow()["permissions"] == {"contents": "read"}
    assert "secrets" not in text
    assert "tap" not in text.replace("touches no tap", "")


def test_fixture_and_workflow_share_a_version_above_any_release():
    version = _workflow()["jobs"]["proof"]["with"]["version"]
    text = FIXTURE.read_text()
    assert version.startswith("999.")
    assert f'version "{version}"' in text
    assert f"cox-{version}.tar.gz" in text


def test_fixture_is_marked_broken_and_carries_no_real_sha256():
    text = FIXTURE.read_text()
    assert "DELIBERATELY BROKEN" in text.splitlines()[0]
    digests = re.findall(r'sha256 "([0-9a-fA-F]+)"', text)
    assert digests
    assert all(set(d) == {"0"} for d in digests)
