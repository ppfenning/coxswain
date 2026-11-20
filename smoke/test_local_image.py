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


@pytest.fixture
def local_postgres() -> None:
    """The pinned Postgres image in the local store, fetched first under the operator's own config when it is absent.

    The wrong belief was "absent at collection means absent": the old skip was decided when the module was collected,
    before the session's `postgres` fixture had pulled the image, so a host meeting it for the first time skipped all
    three tests in a session that then had the image. Docker decides as for the suite's other containers (a missing
    docker skips unless the run requires the suite; a daemon that does not answer fails unless the host opts out); an
    image docker cannot fetch here, as in a session that cannot reach the macOS keychain, skips unless required."""
    broken_ok = plan.is_required(os.environ.get(plan.BROKEN_DOCKER_OK_ENV))
    decision = plan.container_source("the pinned Postgres image", DOCKER, DOCKER is not None and conftest._docker_ok(DOCKER),
                                     required=conftest._required(), broken_docker_ok=broken_ok)
    if isinstance(decision, plan.Skip | plan.Fail):
        conftest._unavailable(decision)
    try:
        conftest._pull(plan.POSTGRES_IMAGE)
    except RuntimeError as exc:
        conftest._unavailable(plan.unavailable(f"the pinned Postgres image is not in the local store: {exc}",
                                               required=conftest._required()))


@pytest.fixture
def no_credentials(local_postgres: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A docker client config whose credential helper cannot run, as in a non-interactive macOS session. The pinned
    image is made present before the swap, under the real config."""
    (tmp_path / "config.json").write_text(json.dumps({"credsStore": "smoke-absent-helper"}), encoding="utf-8")
    if ENDPOINT is not None:
        monkeypatch.setenv("DOCKER_HOST", ENDPOINT)
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    return tmp_path


def test_the_swapped_config_still_reaches_the_operators_daemon(no_credentials: Path) -> None:
    # Before the fix, on macOS, this dialled /var/run/docker.sock: the swapped config had dropped Docker Desktop's context.
    swapped = _daemon_id()
    assert swapped.returncode == 0, swapped.stderr
    assert swapped.stdout.strip() == REAL_DAEMON_ID


def test_inspect_resolves_a_present_digest_pinned_image_without_credentials(no_credentials: Path) -> None:
    done = subprocess.run(plan.docker_inspect_argv(plan.POSTGRES_IMAGE), capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().startswith("sha256:")


def test_the_fixture_pull_uses_a_present_digest_pinned_image_without_credentials(no_credentials: Path) -> None:
    # Before the fix this ran `docker pull`, which asked the absent helper for credentials and failed every attempt.
    conftest._pull(plan.POSTGRES_IMAGE)
