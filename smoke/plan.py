"""The smoke suite's pure core: every decision and every command it builds, with no I/O, no clock and no environment.

`smoke/conftest.py` is the shell. It gathers inputs (what is on PATH, the environment, file hashes), calls these
functions and runs what they return. The functions are unit-tested in `tests/test_smoke_plan.py`; the shell is proven
by the smoke tests themselves.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

POSTGRES_IMAGE = "postgres:16"
POSTGRES_PASSWORD = "smoke"
CONTAINER_LABEL = "coxswain-smoke=1"
# The environment variable a CI job (or a person) sets to point the suite at a Postgres it already runs.
URL_ENV = "COX_SMOKE_POSTGRES_URL"
# Set in CI: a missing docker, uv or Postgres fails the suite instead of skipping it, so a required job cannot pass empty.
REQUIRED_ENV = "COX_SMOKE_REQUIRED"
# The only hosts the suite will open a store on. The suite never touches a shared or live store.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# What a sandboxed `cox` inherits from the caller's environment; everything else (HOME, XDG_*, PYTHONPATH,
# VIRTUAL_ENV, AGENT_TOOLS_PROFILE, COXSWAIN_*) is left behind so the command reads only the sandbox.
INHERITED_KEYS = ("LANG", "LC_ALL", "TZ", "TERM", "TMPDIR", "SYSTEMROOT")


@dataclass(frozen=True)
class UseUrl:
    """Open stores on a Postgres that is already running at `url`."""

    url: str


@dataclass(frozen=True)
class StartContainer:
    """Start a throwaway Postgres container with `docker`."""


@dataclass(frozen=True)
class Skip:
    """No Postgres can be had; `reason` says why."""

    reason: str


@dataclass(frozen=True)
class Fail:
    """No Postgres can be had and the run requires one; `reason` says why."""

    reason: str


PostgresSource = UseUrl | StartContainer | Skip | Fail


def is_required(value: str | None) -> bool:
    """`COX_SMOKE_REQUIRED` read as a flag: any non-empty value other than 0, false or no."""
    return bool(value) and value.strip().lower() not in {"0", "false", "no"}


def unavailable(reason: str, *, required: bool) -> Skip | Fail:
    """A missing prerequisite: a failure when the run requires the suite, else a skip that names it."""
    return Fail(reason) if required else Skip(reason)


def loopback_refusal(url: str) -> str | None:
    """Why the suite refuses `url`, or None. Only a loopback postgres URL is taken, so a typo or a copied profile can
    never point the suite at a shared store."""
    parts = urlsplit(url)
    if parts.scheme not in {"postgres", "postgresql"}:
        return f"{URL_ENV} must be a postgresql:// URL, got scheme {parts.scheme or 'none'!r}"
    if (parts.hostname or "") not in LOOPBACK_HOSTS:
        return f"{URL_ENV} must name a loopback host ({', '.join(sorted(LOOPBACK_HOSTS))}), got {parts.hostname!r}"
    return None


def postgres_source(env_url: str | None, docker_path: str | None, docker_ok: bool, *, required: bool) -> PostgresSource:
    """Where the session's Postgres comes from: the URL the environment names, else a container, else skip or fail."""
    if env_url:
        refusal = loopback_refusal(env_url)
        return Fail(refusal) if refusal else UseUrl(env_url)
    if docker_path is None:
        return unavailable(f"no docker on PATH and {URL_ENV} is not set", required=required)
    if not docker_ok:
        return unavailable(f"docker is on PATH but its daemon did not answer, and {URL_ENV} is not set", required=required)
    return StartContainer()


def docker_run_argv(image: str = POSTGRES_IMAGE, password: str = POSTGRES_PASSWORD) -> list[str]:
    """`docker run` for a throwaway Postgres: removed on stop, on a random loopback port, data on tmpfs and no fsync,
    since nothing in it outlives the session."""
    return [
        "docker", "run", "--detach", "--rm",
        "--label", CONTAINER_LABEL,
        "--env", f"POSTGRES_PASSWORD={password}",
        "--env", "POSTGRES_INITDB_ARGS=--no-sync",
        "--publish", "127.0.0.1::5432",
        "--tmpfs", "/var/lib/postgresql/data",
        image,
        "-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off",
    ]


def docker_ready_argv(container: str) -> list[str]:
    """Readiness over TCP, which answers only once the entrypoint's init server has stopped and the real one listens."""
    return ["docker", "exec", container, "pg_isready", "--username", "postgres", "--host", "127.0.0.1"]


def docker_port_argv(container: str) -> list[str]:
    return ["docker", "port", container, "5432/tcp"]


def docker_remove_argv(container: str) -> list[str]:
    return ["docker", "rm", "--force", container]


def parse_published_port(output: str) -> int:
    """The host port from `docker port` output (`127.0.0.1:32768`, one line per address). Raises ValueError on none."""
    for line in output.splitlines():
        match = re.search(r":(\d+)\s*$", line.strip())
        if match:
            return int(match.group(1))
    raise ValueError(f"no published port in {output!r}")


def postgres_url(port: int, *, database: str = "postgres", password: str = POSTGRES_PASSWORD, host: str = "127.0.0.1") -> str:
    return f"postgresql://postgres:{quote(password, safe='')}@{host}:{port}/{database}"


def with_database(url: str, database: str) -> str:
    """`url` with its path replaced by `database`; the query string, if any, is kept."""
    parts = urlsplit(url)
    return parts._replace(path=f"/{database}").geturl()


def database_name(test_id: str) -> str:
    """A Postgres database name for one test: lower case letters, digits and underscores, at most 63 characters, and
    distinct for distinct test ids (a hash of the id closes it)."""
    stem = re.sub(r"[^a-z0-9]+", "_", test_id.lower()).strip("_")[:40]
    digest = hashlib.sha256(test_id.encode()).hexdigest()[:12]
    return f"smoke_{stem}_{digest}"


def sandbox_env(
    inherited: Mapping[str, str], *, home: str, bin_dirs: Sequence[str], system_path: str, extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The whole environment a sandboxed command runs with. HOME and every XDG root sit inside the sandbox, git reads
    only the sandbox's config, PATH is the install's bin first and then the system's, and only `INHERITED_KEYS`
    pass through from the caller."""
    kept = {key: inherited[key] for key in INHERITED_KEYS if key in inherited}
    return {
        **kept,
        "HOME": home,
        "XDG_CONFIG_HOME": f"{home}/.config",
        "XDG_DATA_HOME": f"{home}/.local/share",
        "XDG_STATE_HOME": f"{home}/.local/state",
        "XDG_CACHE_HOME": f"{home}/.cache",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": f"{home}/.gitconfig",
        "GIT_TERMINAL_PROMPT": "0",
        "PATH": ":".join([*bin_dirs, system_path]),
        **(extra or {}),
    }


def gitconfig_text(name: str = "Smoke Test", email: str = "smoke@example.invalid") -> str:
    return f"[user]\n\tname = {name}\n\temail = {email}\n[init]\n\tdefaultBranch = main\n[advice]\n\tdetachedHead = false\n"


def routing_profile_text(*, team: str, workspace: str, harness_dir: str, provider_profile: str) -> str:
    """The routing profile `cox` reads at `~/.config/agent-tools/profile.yaml`."""
    return (
        f"team: {team}\n"
        f"workspace_dir: {workspace}\n"
        f"harness_dir: {harness_dir}\n"
        f"provider_profile: {provider_profile}\n"
    )


def provider_profile_text(storage_url: str) -> str:
    """The provider profile the routing profile names: only the store."""
    return f"storage_url: {storage_url}\n"


def work_item_text(fields: Mapping[str, str], body: str) -> str:
    """A work item file: YAML front matter with `fields` in the order given, then the body."""
    front = "".join(f"{key}: {value}\n" for key, value in fields.items())
    return f"---\n{front}---\n\n{body.rstrip()}\n"


def front_matter(text: str) -> dict[str, str]:
    """The flat `key: value` lines of a work item's front matter; nested values are not read."""
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return {}
    end = next((i for i, line in enumerate(lines[1:], 1) if line == "---"), None)
    if end is None:
        return {}
    pairs = (line.split(":", 1) for line in lines[1:end] if ":" in line and not line.startswith((" ", "-")))
    return {key.strip(): value.strip() for key, value in pairs}


def install_drift(tree: Mapping[str, str], installed: Mapping[str, str]) -> list[str]:
    """The relative paths whose content hash differs between the tree and the install, or that only one side has.
    Empty means the install is this tree."""
    return sorted(path for path in tree.keys() | installed.keys() if tree.get(path) != installed.get(path))


def python_sources(paths: Iterable[str]) -> list[str]:
    """The `.py` files among `paths`, skipping caches, sorted."""
    return sorted(p for p in paths if p.endswith(".py") and "__pycache__" not in p.split("/"))


def package_dirs(packages: Sequence[str], package_dir: Mapping[str, str]) -> dict[str, tuple[str, str]]:
    """Each setuptools package as (its folder in the tree, its folder in site-packages), from pyproject's `packages`
    and `package-dir`. A top-level package with no `package-dir` entry sits at the tree root under its own name."""
    out = {}
    for package in packages:
        top, *rest = package.split(".")
        tree_dir = "/".join([package_dir.get(top, top), *rest])
        out[package] = (tree_dir, "/".join([top, *rest]))
    return out
