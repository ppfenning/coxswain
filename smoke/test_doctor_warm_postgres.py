"""`smoke postgres`: the doctor's advisory row for the warm smoke Postgres (its judgement has unit tests in
src/tools/tests/test_smoke_warm.py). The report-only test runs wherever the suite runs. The `--fix` test runs the real
docker commands, so it needs a docker that answers and skips with that reason where it does not."""

import json
import shutil
import subprocess

import pytest

CONTAINER = "coxswain-smoke-warm-postgres"
PINNED = "server up on the pinned image"


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=300, check=False)


def _state() -> tuple[str, str] | None:
    """The warm container's (status, image ref) on the host's docker, or None when there is none."""
    done = _docker("inspect", "--format", "{{.State.Status}} {{.Config.Image}}", CONTAINER)
    return tuple(done.stdout.split(maxsplit=1)) if done.returncode == 0 else None


def _row(sandbox, *flags: str, env=None) -> dict:
    done = subprocess.run(
        [str(sandbox.install.cox), "setup", "doctor", *flags, "--json"],
        env=env or sandbox.env, cwd=sandbox.home, capture_output=True, text=True, timeout=600, check=False,
    )
    # The doctor's exit code follows its other rows, which a sandbox may fail; stdout is the JSON alone either way.
    row = {row["check"]: row for row in json.loads(done.stdout)["rows"]}["smoke postgres"]
    # What the doctor printed about its docker commands goes to stderr under --json; keep it for a failing assertion.
    return {**row, "stderr": done.stderr}


def test_the_doctor_reports_the_warm_smoke_postgres(sandbox):
    row = _row(sandbox)
    assert row["ok"] is True
    # Which state it is depends on the host's docker; each one names its next step or the fallback.
    assert any(
        row["detail"].startswith(start)
        for start in ("server up on the pinned image", "present", "absent", "docker is not available", "docker's answer")
    ), row["detail"]


def test_fix_brings_the_warm_container_up_on_the_pinned_image_and_puts_it_back(sandbox):
    if shutil.which("docker") is None or _docker("info").returncode != 0:
        pytest.skip("docker does not answer on this host, so the warm container cannot be started")
    # The sandbox drops DOCKER_HOST, and a desktop docker's socket is named by its context, not /var/run/docker.sock.
    endpoint = _docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}").stdout.strip()
    env = {**sandbox.env, "DOCKER_HOST": endpoint} if endpoint else sandbox.env
    before = _state()
    if before is not None and before[0] == "running":
        assert _docker("stop", CONTAINER).returncode == 0
    elif before is not None and not before[1].startswith("postgres:16@sha256:"):
        pytest.skip(f"the host's {CONTAINER} runs {before[1]}, not the pinned image; the test will not replace it")
    try:
        report = _row(sandbox, env=env)
        assert "`cox setup doctor --fix`" in report["detail"], (report["detail"], report["stderr"])
        fixed = _row(sandbox, "--fix", env=env)
        assert fixed["ok"] is True
        assert fixed["detail"].startswith(PINNED), (fixed["detail"], fixed["stderr"])
        assert _state()[0] == "running"
    finally:
        # Leave the host as found: a container that was running runs again, a stopped one is stopped, an absent one is gone.
        if before is None:
            _docker("rm", "--force", CONTAINER)
        elif before[0] == "running":
            _docker("start", CONTAINER)
        else:
            _docker("stop", CONTAINER)
