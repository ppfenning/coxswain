"""`smoke postgres`: the doctor's advisory row for the warm smoke Postgres (its judgement has unit tests in
src/tools/tests/test_smoke_warm.py).

Every test here aims the doctor at a throwaway container name and a port the test picked itself
(`COX_SMOKE_WARM_CONTAINER`, `COX_SMOKE_WARM_PORT`). The real warm container lives on tmpfs, and stopping it would drop
every database on it, including ones other lanes and this session's own workers are using, so no test here reads,
stops or removes it. The `--fix` test runs the real docker commands, so it needs a docker that answers and skips with
that reason where it does not."""

import json
import shutil
import socket
import subprocess
import uuid

import pytest

PINNED = "server up on the pinned image"
WARM_LABEL = "coxswain-smoke-warm=1"


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=300, check=False)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def throwaway():
    """A container name no one else uses and a free loopback port; whatever the doctor made under that name goes."""
    name = f"coxswain-smoke-warm-test-{uuid.uuid4().hex[:12]}"
    yield name, _free_port()
    if shutil.which("docker"):
        _docker("rm", "--force", name)


def _row(sandbox, env, *flags: str) -> dict:
    done = subprocess.run(
        [str(sandbox.install.cox), "setup", "doctor", *flags, "--json"],
        env=env, cwd=sandbox.home, capture_output=True, text=True, timeout=600, check=False,
    )
    # The doctor's exit code follows its other rows, which a sandbox may fail; stdout is the JSON alone either way.
    row = {row["check"]: row for row in json.loads(done.stdout)["rows"]}["smoke postgres"]
    # What the doctor printed about its docker commands goes to stderr under --json; keep it for a failing assertion.
    return {**row, "stderr": done.stderr}


def _env(sandbox, name: str, port: int, docker_host: str | None = None) -> dict:
    env = {**sandbox.env, "COX_SMOKE_WARM_CONTAINER": name, "COX_SMOKE_WARM_PORT": str(port)}
    return {**env, "DOCKER_HOST": docker_host} if docker_host else env


def test_the_doctor_reports_an_absent_container_without_starting_it(sandbox, throwaway):
    name, port = throwaway
    row = _row(sandbox, _env(sandbox, name, port))
    assert row["ok"] is True
    assert row["detail"].startswith(("absent", "docker is not available")), row["detail"]


def test_fix_waits_for_a_held_port_then_brings_the_container_up(sandbox, throwaway):
    if shutil.which("docker") is None or _docker("info").returncode != 0:
        pytest.skip("docker does not answer on this host, so the throwaway container cannot be started")
    name, port = throwaway
    # The sandbox drops DOCKER_HOST, and a desktop docker's socket is named by its context, not /var/run/docker.sock.
    endpoint = _docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}").stdout.strip()
    env = _env(sandbox, name, port, endpoint or None)

    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", port))
        squatter.listen()
        held = _row(sandbox, env, "--fix")
    assert held["detail"].startswith(f"port 127.0.0.1:{port} is held by something else"), (held["detail"], held["stderr"])
    assert _docker("inspect", name).returncode != 0, "the doctor created a container while the port was held"

    fixed = _row(sandbox, env, "--fix")
    assert fixed["ok"] is True
    assert fixed["detail"] == (
        f"{PINNED}; set COX_SMOKE_POSTGRES_URL=postgresql://postgres:smoke@127.0.0.1:{port}/postgres "
        "in the worker's unit environment"
    ), (fixed["detail"], fixed["stderr"])
    state = _docker("inspect", "--format", '{{.State.Status}} {{index .Config.Labels "coxswain-smoke-warm"}}', name)
    assert state.stdout.split() == ["running", WARM_LABEL.partition("=")[2]]

    # Idempotent: a second --fix runs no docker command and reads the same.
    again = _row(sandbox, env, "--fix")
    assert again["detail"] == fixed["detail"]
    assert "smoke postgres: docker" not in again["stderr"]
