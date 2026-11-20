"""The warm smoke Postgres: one long-lived container per worker machine, kept by `cox setup doctor`.

Pure core. The suite already reaches any loopback server named by COX_SMOKE_POSTGRES_URL without starting docker, so
this module only says what the container is, what its row reads, and which docker commands `--fix` runs. The edge
that runs them is `cli_doctor._doctor_smoke_postgres_rows`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# The same digest literal as smoke/plan.py POSTGRES_IMAGE. Bump both together; a test in tests/test_smoke_plan.py
# reads this file's text and fails when they differ.
IMAGE = "postgres:16@sha256:dd2bf3ce2dc83a81674cf7f217fcfd8a5009a4b325dda5b843bfee82a06cff6f"
CONTAINER = "coxswain-smoke-warm-postgres"
PORT = 55432
USER = "postgres"
PASSWORD = "smoke"
# Not `coxswain-smoke=1`, the label the per-session reaper (`stale_containers`) lists by, so it never removes this one.
LABEL = "coxswain-smoke-warm=1"
URL_ENV = "COX_SMOKE_POSTGRES_URL"
ROW = "smoke postgres"
FIX_HINT = "`cox setup doctor --fix`"
# Set to anything to make the doctor skip this row without running a docker command, as the unit suites do.
SKIP_ENV = "COX_DOCTOR_SKIP_SMOKE_POSTGRES"
# Container states `docker start` moves to running.
STARTABLE = ("exited", "created")


@dataclass(frozen=True)
class Inspected:
    state: str
    image: str


# What `parse_inspect` returns for a reply it cannot read: not the same as an absent container, which is None.
UNREADABLE = Inspected("unreadable", "")


def url() -> str:
    """The value a worker sets as COX_SMOKE_POSTGRES_URL."""
    return f"postgresql://{USER}:{PASSWORD}@127.0.0.1:{PORT}/postgres"


def run_argv() -> list[str]:
    """Today's throwaway container (loopback publish, data on tmpfs, no fsync), except: no `--rm`, a restart policy,
    a fixed port and its own label key."""
    return [
        "docker", "run", "--detach",
        "--name", CONTAINER,
        "--restart", "unless-stopped",
        "--label", LABEL,
        "--env", f"POSTGRES_PASSWORD={PASSWORD}",
        "--env", "POSTGRES_INITDB_ARGS=--no-sync",
        "--publish", f"127.0.0.1:{PORT}:5432",
        "--tmpfs", "/var/lib/postgresql/data",
        IMAGE,
        "-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off",
    ]


def inspect_argv() -> list[str]:
    return ["docker", "inspect", CONTAINER]


def ready_argv() -> list[str]:
    """Readiness over TCP, which answers only once the entrypoint's init server has stopped and the real one listens."""
    return ["docker", "exec", CONTAINER, "pg_isready", "--username", USER, "--host", "127.0.0.1"]


def docker_answered(returncode: int, stderr: str) -> bool:
    """Whether `docker inspect` reached a daemon: it succeeded, or the daemon said the container does not exist."""
    # "no such file or directory" is a socket that is not there, which is no daemon at all: match docker's own wording.
    lowered = stderr.lower()
    return returncode == 0 or "no such object" in lowered or "no such container" in lowered


def parse_inspect(text: str) -> Inspected | None:
    """The state and image ref of the first container in `docker inspect` output; None for an empty list, text that
    is not JSON, or an entry without them."""
    try:
        parsed = json.loads(text)
    except ValueError:
        return UNREADABLE
    if parsed == []:
        return None
    first = parsed[0] if isinstance(parsed, list) else None
    if not isinstance(first, dict):
        return UNREADABLE
    state, config = first.get("State"), first.get("Config")
    status = state.get("Status") if isinstance(state, dict) else None
    image = config.get("Image") if isinstance(config, dict) else None
    if isinstance(status, str) and isinstance(image, str):
        return Inspected(status, image)
    return UNREADABLE


def fix_argvs(found: Inspected | None) -> list[list[str]]:
    """The docker commands `--fix` runs: replace a container on another image, start a stopped one, create an absent
    one, and nothing for one that is running on the pinned image."""
    if found is None:
        return [run_argv()]
    if found == UNREADABLE:
        return []
    if found.image != IMAGE or found.state == "dead":
        return [["docker", "rm", "--force", CONTAINER], run_argv()]
    if found.state in STARTABLE:
        return [["docker", "start", CONTAINER]]
    if found.state == "paused":
        return [["docker", "unpause", CONTAINER]]
    return []  # running, or restarting or removing: docker is already on it


def skipped_row() -> dict:
    return {"check": ROW, "ok": True, "detail": f"skipped: {SKIP_ENV} is set"}


def build_row(found: Inspected | None, docker_ok: bool) -> dict:
    """Advisory: `ok` is True in every state, so `exit_code` ignores the row and an upgrade is never rolled back for it."""
    if not docker_ok:
        detail = "docker is not available; the suite will start a container per session"
    elif found is None:
        detail = f"absent; {FIX_HINT} creates it (without --fix the doctor starts nothing, and the suite starts a container per session)"
    elif found == UNREADABLE:
        detail = "docker's answer about the container could not be read; --fix does nothing until it can be"
    elif found.image != IMAGE:
        detail = f"present on {found.image}, not the pinned image; {FIX_HINT} replaces it"
    elif found.state in STARTABLE:
        detail = f"present but {found.state}; {FIX_HINT} starts it (without --fix the doctor starts nothing)"
    elif found.state == "paused":
        detail = f"present but paused; {FIX_HINT} unpauses it (without --fix the doctor starts nothing)"
    elif found.state == "dead":
        detail = f"present but dead; {FIX_HINT} replaces it"
    elif found.state != "running":
        detail = f"present and {found.state}; docker is bringing it up or down, so --fix does nothing; run the doctor again"
    else:
        detail = f"server up on the pinned image; set {URL_ENV}={url()} in the worker's unit environment"
    return {"check": ROW, "ok": True, "detail": detail}
