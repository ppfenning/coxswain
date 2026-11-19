"""A digest-pinned image already in the local store is used without `docker pull`.

On a macOS chair lane the session cannot reach the keychain, so every `docker pull` fails in Docker Desktop's
credential helper (`keychain cannot be accessed because the current session does not allow user interaction`, or
`docker-credential-desktop: executable file not found`), even for an image that is present. These tests rebuild that
session on any host: a `DOCKER_CONFIG` whose `credsStore` names a helper that does not exist, so anything that consults
credentials fails the way the lane did.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from smoke import conftest, plan

DOCKER = shutil.which("docker")


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
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    return tmp_path


@needs_local_postgres
def test_inspect_resolves_a_present_digest_pinned_image_without_credentials(no_credentials: Path) -> None:
    done = subprocess.run(plan.docker_inspect_argv(plan.POSTGRES_IMAGE), capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().startswith("sha256:")


@needs_local_postgres
def test_the_fixture_pull_uses_a_present_digest_pinned_image_without_credentials(no_credentials: Path) -> None:
    # Before the fix this ran `docker pull`, which asked the absent helper for credentials and failed every attempt.
    conftest._pull(plan.POSTGRES_IMAGE)
