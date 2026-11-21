"""The warm smoke Postgres: one long-lived container per worker machine, kept by `cox setup doctor`.

Pure core. The suite already reaches any loopback server named by COX_SMOKE_POSTGRES_URL without starting docker, so
this module only says what the container is, whether a container docker reports is ours, what its row reads, and
which docker commands `--fix` runs. The edge that runs them is `cli_doctor._doctor_smoke_postgres_rows`.

The container's name and host port are a `Target`. Production uses `DEFAULT`; the two variables in `target_from` let
a test aim the doctor at a throwaway container on a port of its own, so it never touches the real warm one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

# The same digest literal as smoke/plan.py POSTGRES_IMAGE. Bump both together; a test in tests/test_smoke_plan.py
# reads this file's text and fails when they differ.
IMAGE = "postgres:16@sha256:dd2bf3ce2dc83a81674cf7f217fcfd8a5009a4b325dda5b843bfee82a06cff6f"
CONTAINER = "coxswain-smoke-warm-postgres"
PORT = 55432
HOST = "127.0.0.1"
USER = "postgres"
PASSWORD = "smoke"
# Not `coxswain-smoke=1`, the label the per-session reaper (`stale_containers`) lists by, so it never removes this one.
LABEL = "coxswain-smoke-warm=1"
URL_ENV = "COX_SMOKE_POSTGRES_URL"
ROW = "smoke postgres"
FIX_HINT = "`cox setup doctor --fix`"
# Set to anything to make the doctor skip this row without running a docker command, as the unit suites do.
SKIP_ENV = "COX_DOCTOR_SKIP_SMOKE_POSTGRES"
# Name and host port of the container the doctor keeps, for a test's throwaway one. Unset in production.
CONTAINER_ENV = "COX_SMOKE_WARM_CONTAINER"
PORT_ENV = "COX_SMOKE_WARM_PORT"
# Container states `docker start` moves to running.
STARTABLE = ("exited", "created")


@dataclass(frozen=True)
class Target:
    name: str
    port: int


DEFAULT = Target(CONTAINER, PORT)


@dataclass(frozen=True)
class Inspected:
    state: str
    image: str
    name: str = ""
    labels: tuple[tuple[str, str], ...] = ()
    # Where the container's 5432/tcp is published, as (host ip, host port) pairs from HostConfig.PortBindings.
    bindings: tuple[tuple[str, str], ...] = ()


# What `parse_inspect` returns for a reply it cannot read: not the same as an absent container, which is None.
UNREADABLE = Inspected("unreadable", "")


def target_from(env: Mapping[str, str]) -> Target | str:
    """The container the doctor keeps: `DEFAULT`, or the one the two variables name. A port that is not a number in
    1..65535 is the reason the row gives instead."""
    name = env.get(CONTAINER_ENV) or CONTAINER
    raw = env.get(PORT_ENV)
    if not raw:
        return Target(name, PORT)
    if not raw.isdigit() or not 0 < int(raw) < 65536:
        return f"{PORT_ENV}={raw!r} is not a port number"
    return Target(name, int(raw))


def url(target: Target = DEFAULT) -> str:
    """The value a worker sets as COX_SMOKE_POSTGRES_URL."""
    return f"postgresql://{USER}:{PASSWORD}@{HOST}:{target.port}/postgres"


def run_argv(target: Target = DEFAULT) -> list[str]:
    """Today's throwaway container (loopback publish, data on tmpfs, no fsync), except: no `--rm`, a restart policy,
    a fixed port and its own label key."""
    return [
        "docker", "run", "--detach",
        "--name", target.name,
        "--restart", "unless-stopped",
        "--label", LABEL,
        "--env", f"POSTGRES_PASSWORD={PASSWORD}",
        "--env", "POSTGRES_INITDB_ARGS=--no-sync",
        "--publish", f"{HOST}:{target.port}:5432",
        "--tmpfs", "/var/lib/postgresql/data",
        IMAGE,
        "-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off",
    ]


def remove_argv(target: Target = DEFAULT) -> list[str]:
    return ["docker", "rm", "--force", target.name]


def inspect_argv(target: Target = DEFAULT) -> list[str]:
    return ["docker", "inspect", "--type", "container", target.name]


def ready_argv(target: Target = DEFAULT) -> list[str]:
    """Readiness over TCP, which answers only once the entrypoint's init server has stopped and the real one listens."""
    return ["docker", "exec", target.name, "pg_isready", "--username", USER, "--host", HOST]


def docker_answered(returncode: int, stderr: str) -> bool:
    """Whether `docker inspect` reached a daemon: it succeeded, or the daemon said the container does not exist."""
    # "no such file or directory" is a socket that is not there, which is no daemon at all: match docker's own wording.
    lowered = stderr.lower()
    return returncode == 0 or "no such object" in lowered or "no such container" in lowered


def _pairs(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict):
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in value.items()))


def _bindings(host_config: object) -> tuple[tuple[str, str], ...]:
    ports = host_config.get("PortBindings") if isinstance(host_config, dict) else None
    entries = ports.get("5432/tcp") if isinstance(ports, dict) else None
    if not isinstance(entries, list):
        return ()
    return tuple(sorted((str(e.get("HostIp", "")), str(e.get("HostPort", ""))) for e in entries if isinstance(e, dict)))


def parse_inspect(text: str) -> Inspected | None:
    """The first container in `docker inspect` output; None for an empty list, UNREADABLE for text that is not JSON
    or an entry without a state and an image."""
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
    if not (isinstance(status, str) and isinstance(image, str)):
        return UNREADABLE
    name = first.get("Name")
    labels = config.get("Labels") if isinstance(config, dict) else None
    return Inspected(status, image, name if isinstance(name, str) else "", _pairs(labels), _bindings(first.get("HostConfig")))


def is_ours(found: Inspected, target: Target) -> bool:
    """A container the doctor created for `target`: its name and its label. Anything else under that name is someone
    else's, and the doctor neither points smoke at it nor removes it."""
    key, _, value = LABEL.partition("=")
    return found.name.lstrip("/") == target.name and (key, value) in found.labels


def publishes(found: Inspected, target: Target) -> bool:
    """Whether the container's 5432 is published on the URL's address and on nothing else, so the URL reaches it."""
    return found.bindings == ((HOST, str(target.port)),)


def fix_argvs(found: Inspected | None, target: Target = DEFAULT, *, port_free: bool = True) -> list[list[str]]:
    """The docker commands `--fix` runs: create an absent one, start a stopped one, replace one of ours that is on
    another image, published elsewhere or dead, and nothing for one that is running as it should. Nothing touches a
    container that is not ours, and nothing that would bind the port runs while something else holds it."""
    if found is None:
        return [run_argv(target)] if port_free else []
    if found == UNREADABLE or not is_ours(found, target):
        return []
    # A running container published on the target port holds it itself; any other one needs it free.
    holds_port = found.state in ("running", "paused") and publishes(found, target)
    if found.image != IMAGE or found.state == "dead" or not publishes(found, target):
        return [remove_argv(target), run_argv(target)] if port_free or holds_port else []
    if found.state in STARTABLE:
        return [["docker", "start", target.name]] if port_free else []
    if found.state == "paused":
        return [["docker", "unpause", target.name]]
    return []  # running, or restarting or removing: docker is already on it


def cleanup_argvs(found: Inspected | None, target: Target = DEFAULT) -> list[list[str]]:
    """After a failed `docker run`: remove the container it left in `created`, when it is ours. A later `--fix` then
    starts from absent instead of starting a container whose port could not be bound."""
    if found is not None and found != UNREADABLE and found.state == "created" and is_ours(found, target):
        return [remove_argv(target)]
    return []


def skipped_row() -> dict:
    return {"check": ROW, "ok": True, "detail": f"skipped: {SKIP_ENV} is set"}


def refused_row(reason: str) -> dict:
    return {"check": ROW, "ok": True, "detail": f"{reason}; the doctor runs no docker command"}


def build_row(found: Inspected | None, docker_ok: bool, target: Target = DEFAULT, *, port_free: bool = True) -> dict:
    """Advisory: `ok` is True in every state, so `exit_code` ignores the row and an upgrade is never rolled back for it.
    Only a running container that is ours, on the pinned image and published on the URL's address gives the URL."""
    if not docker_ok:
        detail = "docker is not available; the suite will start a container per session"
    elif found == UNREADABLE:
        detail = "docker's answer about the container could not be read; --fix does nothing until it can be"
    elif found is not None and not is_ours(found, target):
        detail = (f"a container named {target.name} is there that the doctor did not create (no {LABEL} label); "
                  "the doctor leaves it alone and gives no URL")
    elif fix_argvs(found, target) and not fix_argvs(found, target, port_free=port_free):
        detail = f"port {HOST}:{target.port} is held by something else, so --fix starts nothing; free it, or the suite starts a container per session"
    elif found is None:
        detail = f"absent; {FIX_HINT} creates it (without --fix the doctor starts nothing, and the suite starts a container per session)"
    elif found.image != IMAGE:
        detail = f"present on {found.image}, not the pinned image; {FIX_HINT} replaces it"
    elif not publishes(found, target):
        shown = ", ".join(f"{ip}:{port}" for ip, port in found.bindings) or "nothing"
        detail = f"present but published on {shown}, not {HOST}:{target.port}; {FIX_HINT} replaces it"
    elif found.state in STARTABLE:
        detail = f"present but {found.state}; {FIX_HINT} starts it (without --fix the doctor starts nothing)"
    elif found.state == "paused":
        detail = f"present but paused; {FIX_HINT} unpauses it (without --fix the doctor starts nothing)"
    elif found.state == "dead":
        detail = f"present but dead; {FIX_HINT} replaces it"
    elif found.state != "running":
        detail = f"present and {found.state}; docker is bringing it up or down, so --fix does nothing; run the doctor again"
    else:
        detail = f"server up on the pinned image; set {URL_ENV}={url(target)} in the worker's unit environment"
    return {"check": ROW, "ok": True, "detail": detail}
