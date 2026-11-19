"""A digest-pinned image already in the local store is used without `docker pull`.

On a macOS chair lane the session cannot reach the keychain, so every `docker pull` fails in Docker Desktop's
credential helper (`keychain cannot be accessed because the current session does not allow user interaction`, or
`docker-credential-desktop: executable file not found`), even for an image that is present. These tests rebuild that
session on any host: a `DOCKER_CONFIG` whose `credsStore` names a helper that does not exist, so anything that consults
credentials fails the way the lane did.

A swapped `DOCKER_CONFIG` also drops the operator's docker context. On macOS that context (`desktop-linux`) is what
points the CLI at Docker Desktop's socket under `~/.docker/run`; without it the CLI dials `/var/run/docker.sock`, which
a Mac does not have. So the endpoint is resolved under the real config first (`DOCKER_HOST`, else the active context)
and carried into the swapped one as `DOCKER_HOST`: only the credential helper changes, never the daemon.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from smoke import conftest, plan

DOCKER = shutil.which("docker")


def _endpoint() -> str | None:
    """The daemon endpoint under the operator's real config, read before any test swaps `DOCKER_CONFIG`."""
    if DOCKER is None:
        return None
    done = subprocess.run([DOCKER, *plan.DOCKER_CONTEXT_HOST_ARGV[1:]], capture_output=True, text=True, timeout=30)
    return plan.docker_endpoint(os.environ.get("DOCKER_HOST"), done.stdout if done.returncode == 0 else None)


ENDPOINT = _endpoint()


def _daemon_id() -> subprocess.CompletedProcess[str]:
    return subprocess.run([DOCKER, "info", "--format", "{{.ID}}"], capture_output=True, text=True, timeout=30)


REAL_DAEMON_ID = _daemon_id().stdout.strip() if DOCKER is not None else ""

def _present(image: str) -> bool:
    argv = plan.docker_inspect_argv(image)
    return subprocess.run([DOCKER, *argv[1:]], capture_output=True, text=True, timeout=30).returncode == 0


needs_local_postgres = pytest.mark.skipif(
    DOCKER is None or not conftest._docker_ok(DOCKER) or not _present(plan.POSTGRES_IMAGE),
    reason="needs a docker daemon with the pinned Postgres image already in its local store",
)


@pytest.fixture
def no_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A docker client config whose credential helper cannot run, as in a non-interactive macOS session."""
    (tmp_path / "config.json").write_text(json.dumps({"credsStore": "smoke-absent-helper"}), encoding="utf-8")
    if ENDPOINT is not None:
        monkeypatch.setenv("DOCKER_HOST", ENDPOINT)
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    return tmp_path


@needs_local_postgres
def test_the_swapped_config_still_reaches_the_operators_daemon(no_credentials: Path) -> None:
    # Before the fix, on macOS, this dialled /var/run/docker.sock: the swapped config had dropped Docker Desktop's context.
    swapped = _daemon_id()
    assert swapped.returncode == 0, swapped.stderr
    assert swapped.stdout.strip() == REAL_DAEMON_ID


@needs_local_postgres
def test_inspect_resolves_a_present_digest_pinned_image_without_credentials(no_credentials: Path) -> None:
    done = subprocess.run(plan.docker_inspect_argv(plan.POSTGRES_IMAGE), capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().startswith("sha256:")


@needs_local_postgres
def test_the_fixture_pull_uses_a_present_digest_pinned_image_without_credentials(no_credentials: Path) -> None:
    # Before the fix this ran `docker pull`, which asked the absent helper for credentials and failed every attempt.
    conftest._pull(plan.POSTGRES_IMAGE)
