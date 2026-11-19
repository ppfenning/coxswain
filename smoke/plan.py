"""The smoke suite's pure core: every decision and every command it builds, with no I/O, no clock and no environment.

`smoke/conftest.py` is the shell. It gathers inputs (what is on PATH, the environment, file hashes), calls these
functions and runs what they return. The functions are unit-tested in `tests/test_smoke_plan.py`; the shell is proven
by the smoke tests themselves.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, urlsplit

# Pinned by digest (the multi-arch index of postgres:16), here and in CI's service, so a re-pushed tag cannot change
# what the suite runs against. Bump both together.
POSTGRES_IMAGE = "postgres:16@sha256:dd2bf3ce2dc83a81674cf7f217fcfd8a5009a4b325dda5b843bfee82a06cff6f"
POSTGRES_PASSWORD = "smoke"
LABEL = "coxswain-smoke"
CONTAINER_LABEL = f"{LABEL}=1"
# Each container also carries its session id and its start time (epoch seconds), so a later session reaps only
# containers a killed session left behind, never one another session is still using.
SESSION_LABEL = f"{LABEL}.session"
STARTED_LABEL = f"{LABEL}.started"
STALE_AFTER_S = 2 * 60 * 60
# The environment variable a CI job (or a person) sets to point the suite at a Postgres it already runs.
URL_ENV = "COX_SMOKE_POSTGRES_URL"
# Set in CI: a missing docker, uv or Postgres fails the suite instead of skipping it, so a required job cannot pass empty.
REQUIRED_ENV = "COX_SMOKE_REQUIRED"
# Set on a host whose docker is known not to work: a docker on PATH whose daemon does not answer then skips the
# suite instead of failing it. Without it that case fails, so a lane cannot pass a broken docker off as no docker.
BROKEN_DOCKER_OK_ENV = "COX_SMOKE_SKIP_BROKEN_DOCKER"
# The only hosts the suite will open a store on. The suite never touches a shared or live store.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# libpq connection parameters that name or resolve the host, which a URL may carry in its query string.
HOST_PARAMETERS = frozenset({"host", "hostaddr", "service"})
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
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        return f"{URL_ENV} does not parse as a URL: {exc}"
    if parts.scheme not in {"postgres", "postgresql"}:
        return f"{URL_ENV} must be a postgresql:// URL, got scheme {parts.scheme or 'none'!r}"
    # libpq takes several hosts in the netloc (`a:5432,b:5432`) and lets the query override the netloc's host
    # (`host=`, `hostaddr=`, or a `service=` resolved from a service file), so each of those is refused outright.
    if "," in parts.netloc:
        return f"{URL_ENV} must name one host, got several in {parts.netloc.rpartition('@')[2]!r}"
    overrides = sorted({key.lower() for key, _ in parse_qsl(parts.query, keep_blank_values=True)} & HOST_PARAMETERS)
    if overrides:
        return f"{URL_ENV} must not set its host in the query, got {', '.join(overrides)}"
    try:
        host = parts.hostname or ""
    except ValueError:
        host = ""
    if host not in LOOPBACK_HOSTS:
        return f"{URL_ENV} must name a loopback host ({', '.join(sorted(LOOPBACK_HOSTS))}), got {host or None!r}"
    return None


def postgres_source(
    env_url: str | None, docker_path: str | None, docker_ok: bool, *, required: bool, broken_docker_ok: bool = False,
) -> PostgresSource:
    """Where the session's Postgres comes from: the URL the environment names, else a container, else skip or fail.
    No docker at all skips (unless required). A docker whose daemon does not answer fails, unless `broken_docker_ok`
    and the run is not required: it is a host fault, and a skip would hide it."""
    if env_url:
        refusal = loopback_refusal(env_url)
        return Fail(refusal) if refusal else UseUrl(env_url)
    if docker_path is None:
        return unavailable(f"no docker on PATH and {URL_ENV} is not set", required=required)
    if not docker_ok:
        reason = f"docker is on PATH but its daemon did not answer, and {URL_ENV} is not set"
        if broken_docker_ok and not required:
            return Skip(f"{reason} ({BROKEN_DOCKER_OK_ENV} is set)")
        return Fail(f"{reason}; set {BROKEN_DOCKER_OK_ENV}=1 to skip on this host")
    return StartContainer()


def docker_run_argv(session: str, started: int, image: str = POSTGRES_IMAGE, password: str = POSTGRES_PASSWORD) -> list[str]:
    """`docker run` for a throwaway Postgres: removed on stop, on a random loopback port, data on tmpfs and no fsync,
    since nothing in it outlives the session. Labelled with the session and its start time, for `stale_containers`."""
    return [
        "docker", "run", "--detach", "--rm",
        "--label", CONTAINER_LABEL,
        "--label", f"{SESSION_LABEL}={session}",
        "--label", f"{STARTED_LABEL}={started}",
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
    return docker_port_argv_for(container, 5432)


def docker_port_argv_for(container: str, port: int) -> list[str]:
    """`docker port` for the host address a container's `port` is published on."""
    return ["docker", "port", container, f"{port}/tcp"]


def docker_remove_argv(*containers: str) -> list[str]:
    return ["docker", "rm", "--force", *containers]


def docker_list_argv() -> list[str]:
    """Every smoke container, running or not, as `<id> <started>` lines."""
    return ["docker", "ps", "--all", "--filter", f"label={CONTAINER_LABEL}", "--format", f'{{{{.ID}}}} {{{{.Label "{STARTED_LABEL}"}}}}']


def stale_containers(listing: str, now: int, stale_after: int = STALE_AFTER_S) -> list[str]:
    """The ids in `docker_list_argv` output whose start label is more than `stale_after` seconds old. A container with
    no readable start label is left alone: nothing says it is not some live session's."""
    stale = []
    for line in listing.splitlines():
        container, _, started = line.strip().partition(" ")
        if container and started.strip().isdigit() and now - int(started) > stale_after:
            stale.append(container)
    return stale


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


def routing_profile_text(
    *, team: str, workspace: str, harness_dir: str, provider_profile: str, extra: Mapping[str, str] | None = None,
) -> str:
    """The routing profile `cox` reads at `~/.config/agent-tools/profile.yaml`; `extra` adds flat `key: value` lines."""
    lines = {"team": team, "workspace_dir": workspace, "harness_dir": harness_dir, "provider_profile": provider_profile,
             **(extra or {})}
    return "".join(f"{key}: {value}\n" for key, value in lines.items())


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


def package_data(package_data: Mapping[str, Sequence[str]], dirs: Mapping[str, tuple[str, str]]) -> list[tuple[str, str, str]]:
    """Each `package-data` pattern as (the package's tree folder, its installed folder, the glob), for the packages
    `dirs` knows. A pattern for a package `dirs` does not list is dropped; setuptools would not install it either."""
    return [
        (dirs[package][0], dirs[package][1], pattern)
        for package, patterns in sorted(package_data.items())
        if package in dirs
        for pattern in patterns
    ]


def package_dirs(packages: Sequence[str], package_dir: Mapping[str, str]) -> dict[str, tuple[str, str]]:
    """Each setuptools package as (its folder in the tree, its folder in site-packages), from pyproject's `packages`
    and `package-dir`. A top-level package with no `package-dir` entry sits at the tree root under its own name."""
    out = {}
    for package in packages:
        top, *rest = package.split(".")
        tree_dir = "/".join([package_dir.get(top, top), *rest])
        out[package] = (tree_dir, "/".join([top, *rest]))
    return out


# --- the sandbox forge ----------------------------------------------------------------------------------------------

# Forgejo 11 (the LTS line), pinned by the digest of its multi-arch index so a re-pushed tag cannot change what the
# suite lands through. `cox` reaches it through `agent_tools.forge_forgejo`, the forge `forge_auto` picks for an origin
# on the host `FORGEJO_BASE_URL` names. Pulled from data.forgejo.org, Forgejo's own registry (the same index digest
# codeberg.org serves). Bump the digest on purpose, never by pulling the tag.
FORGEJO_IMAGE = "data.forgejo.org/forgejo/forgejo:11@sha256:946243edbab116d5bb78b73ea68af6f3d69229ba1b1ed958dd82c3481167f3e0"
FORGEJO_PORT = 3000  # the HTTP port inside the container
FORGEJO_UID = "1000:1000"  # the image's `git` user, which owns its data and runs its admin commands
FORGEJO_USER = "smoke"
FORGEJO_EMAIL = "smoke@example.invalid"
# The names forge_forgejo and forge_auto read the server's address and the token from.
FORGEJO_BASE_URL_ENV = "FORGEJO_BASE_URL"
FORGEJO_TOKEN_ENV = "FORGEJO_TOKEN"
# The pytest marker on every test that needs the sandbox forge; CI runs those in a step of their own.
FORGE_MARK = "forge"
COMMIT_STATES = frozenset({"pending", "success", "error", "failure", "warning"})
_TOKEN = re.compile(r"^[0-9a-f]{40}$")


def forge_source(docker_path: str | None, docker_ok: bool, *, required: bool, broken_docker_ok: bool = False) -> StartContainer | Skip | Fail:
    """Whether the session can start the Forgejo container. Docker decides, as it does for Postgres, and there is no
    URL to use instead: the suite creates users, tokens and repositories, which it does only on a server it owns."""
    return container_source("the sandbox forge", docker_path, docker_ok, required=required, broken_docker_ok=broken_docker_ok)


def container_source(
    what: str, docker_path: str | None, docker_ok: bool, *, required: bool, broken_docker_ok: bool = False,
) -> StartContainer | Skip | Fail:
    """Whether the session can start the container for `what` (the sandbox forge, the sandbox object store), which the
    suite only ever uses on a server it owns. Docker decides, as it does for Postgres."""
    if docker_path is None:
        return unavailable(f"no docker on PATH for {what}", required=required)
    if not docker_ok:
        reason = f"docker is on PATH but its daemon did not answer, so {what} cannot start"
        if broken_docker_ok and not required:
            return Skip(f"{reason} ({BROKEN_DOCKER_OK_ENV} is set)")
        return Fail(f"{reason}; set {BROKEN_DOCKER_OK_ENV}=1 to skip on this host")
    return StartContainer()


def forgejo_base_url(port: int, host: str = "127.0.0.1") -> str:
    return f"http://{host}:{port}"


def forgejo_run_argv(session: str, started: int, port: int, image: str = FORGEJO_IMAGE) -> list[str]:
    """`docker run` for a throwaway Forgejo published on `port` of the loopback address only: sqlite on tmpfs, no ssh,
    no sign-up, offline, and its own address as ROOT_URL so the links it returns are ones the suite can reach. The port
    is chosen before the start because ROOT_URL must name it. Labelled as the Postgres container is, so a later session
    reaps it after a killed one."""
    settings = {
        "security__INSTALL_LOCK": "true",
        "database__DB_TYPE": "sqlite3",
        "server__ROOT_URL": f"{forgejo_base_url(port)}/",
        "server__HTTP_PORT": str(FORGEJO_PORT),
        "server__DISABLE_SSH": "true",
        "server__OFFLINE_MODE": "true",
        "service__DISABLE_REGISTRATION": "true",
        # The suite plays the CI through commit statuses; with Actions on, a pushed .github/workflows would add pending
        # statuses no runner ever finishes.
        "actions__ENABLED": "false",
        "repository__DEFAULT_BRANCH": "main",
        "log__LEVEL": "Warn",
    }
    return [
        "docker", "run", "--detach", "--rm",
        "--label", CONTAINER_LABEL,
        "--label", f"{SESSION_LABEL}={session}",
        "--label", f"{STARTED_LABEL}={started}",
        *[arg for key, value in settings.items() for arg in ("--env", f"FORGEJO__{key}={value}")],
        "--publish", f"127.0.0.1:{port}:{FORGEJO_PORT}",
        "--tmpfs", "/data:exec",  # docker mounts a tmpfs noexec by default, and Forgejo runs git hooks from it
        image,
    ]


def docker_pull_argv(image: str) -> list[str]:
    """`docker pull` of a pinned image; the forge fixture retries it (`PULL_ATTEMPTS`) before the run, so a registry
    hiccup costs a retry rather than the job."""
    return ["docker", "pull", "--quiet", image]


PULL_ATTEMPTS = 3


def forgejo_admin_argv(container: str, *args: str) -> list[str]:
    """`forgejo admin ...` inside the container, as the user that owns its data."""
    return ["docker", "exec", "--user", FORGEJO_UID, container, "forgejo", "admin", *args]


def forgejo_user_argv(container: str, password: str, user: str = FORGEJO_USER, email: str = FORGEJO_EMAIL) -> list[str]:
    return forgejo_admin_argv(container, "user", "create", "--admin", "--username", user, "--password", password,
                              "--email", email, "--must-change-password=false")


def forgejo_token_argv(container: str, name: str, user: str = FORGEJO_USER) -> list[str]:
    return forgejo_admin_argv(container, "user", "generate-access-token", "--username", user, "--token-name", name,
                              "--scopes", "all", "--raw")


def parse_token(output: str) -> str:
    """The token `generate-access-token --raw` printed: the last line of 40 lower-case hex digits. Raises ValueError
    when there is none, without echoing the output."""
    found = [line.strip() for line in output.splitlines() if _TOKEN.match(line.strip())]
    if not found:
        raise ValueError("generate-access-token printed no token")
    return found[-1]


def forge_remote_url(base_url: str, owner: str, name: str, token: str) -> str:
    """The http origin git pushes to, with the token as the password. `forge_forgejo.parse_remote` drops the
    credentials and keeps `scheme://host:port`, so the API address it derives is the server's own."""
    scheme, _, host = base_url.partition("://")
    return f"{scheme}://{quote(owner, safe='')}:{quote(token, safe='')}@{host}/{owner}/{name}.git"


def forge_env(base_url: str, token: str) -> dict[str, str]:
    """What a sandboxed `cox` needs to reach the forge: the server, for `forge_auto`'s choice, and the token."""
    return {FORGEJO_BASE_URL_ENV: base_url, FORGEJO_TOKEN_ENV: token}


def forge_repo_name(test_id: str) -> str:
    """A repository name for one test: lower-case letters, digits and dashes, distinct for distinct test ids."""
    stem = re.sub(r"[^a-z0-9]+", "-", test_id.lower()).strip("-")[-40:].strip("-")
    return f"{stem}-{hashlib.sha256(test_id.encode()).hexdigest()[:8]}"


def commit_status_body(state: str, context: str, description: str = "") -> dict[str, str]:
    """The body of `POST /repos/{owner}/{repo}/statuses/{sha}`: what a CI job reports for one commit."""
    if state not in COMMIT_STATES:
        raise ValueError(f"not a commit status state: {state!r}")
    return {"state": state, "context": context, "description": description or f"{context}: {state}"}


def uv_env(cache_dir: str, python_dir: str) -> dict[str, str]:
    """What a sandboxed `uv` keeps from the caller: its cache and its managed interpreters, both content-addressed and
    read through, so a sync in the sandbox links what the caller already downloaded instead of fetching it again.
    Its tools and their bin stay in the sandbox (XDG_DATA_HOME, `~/.local/bin`)."""
    return {"UV_CACHE_DIR": cache_dir, "UV_PYTHON_INSTALL_DIR": python_dir}


def tree_files(listing: str) -> list[str]:
    """The paths of `git ls-files -z`: the tracked files, as a clone of this change holds them. An untracked file stays
    out, so a stray local file cannot make the edge tests pass. Sorted, empty names dropped."""
    return sorted({path for path in listing.split("\0") if path})


def closed_port_url(url: str, port: int) -> str:
    """`url` with its port replaced by `port`, a loopback port nothing listens on: a store the doctor cannot reach."""
    parts = urlsplit(url)
    credentials, _, _host = parts.netloc.rpartition("@")
    netloc = f"{credentials}@127.0.0.1:{port}" if credentials else f"127.0.0.1:{port}"
    return parts._replace(netloc=netloc).geturl()


def pull_summary(pull: object) -> dict:
    """What a smoke test checks of a `GET /repos/{owner}/{repo}/pulls/{n}` body: state, merged, head branch and commit,
    and base branch. A body that is not a pull request is `{}`."""
    if not isinstance(pull, dict):
        return {}
    head, base = pull.get("head") or {}, pull.get("base") or {}
    return {"state": pull.get("state"), "merged": pull.get("merged") is True, "head": head.get("ref"),
            "sha": head.get("sha"), "base": base.get("ref")}


# --- the sandbox object store ---------------------------------------------------------------------------------------

# Garage, the S3 server the machines' traces live on, pinned by the digest of its multi-arch index. Bump on purpose.
GARAGE_IMAGE = "dxflrs/garage:v2.1.0@sha256:4c9b34c113e61358466e83fd6e7d66e6d18657ede14b776eb78a93ee8da7cf6a"
GARAGE_S3_PORT = 3900  # the S3 API port inside the container
GARAGE_REGION = "garage"
GARAGE_CONFIG = "/etc/garage.toml"  # where the image's entrypoint reads its config
GARAGE_BUCKET = "smoke-traces"
GARAGE_KEY_NAME = "smoke"
# The variable names the provider profile's object_store block names, as on a person's machine.
GARAGE_ACCESS_KEY_ENV = "GARAGE_ACCESS_KEY_ID"
GARAGE_SECRET_KEY_ENV = "GARAGE_SECRET_ACCESS_KEY"
_NODE_ID = re.compile(r"^([0-9a-f]{64})@")


def garage_config_text(rpc_secret: str) -> str:
    """One node, one copy, sqlite metadata: what a single-node Garage needs. Data and metadata sit on the container's
    tmpfs, so nothing outlives it."""
    return "".join([
        'metadata_dir = "/var/lib/garage/meta"\n',
        'data_dir = "/var/lib/garage/data"\n',
        'db_engine = "sqlite"\n',
        "replication_factor = 1\n",
        'rpc_bind_addr = "[::]:3901"\n',
        'rpc_public_addr = "127.0.0.1:3901"\n',
        f'rpc_secret = "{rpc_secret}"\n',
        "\n[s3_api]\n",
        f's3_region = "{GARAGE_REGION}"\n',
        f'api_bind_addr = "[::]:{GARAGE_S3_PORT}"\n',
    ])


def garage_run_argv(session: str, started: int, config_path: str, image: str = GARAGE_IMAGE) -> list[str]:
    """`docker run` for a throwaway Garage: its S3 port on a random loopback port only, its state on tmpfs, its config
    mounted read-only. Labelled as the Postgres container is, so a later session reaps it after a killed one."""
    return [
        "docker", "run", "--detach", "--rm",
        "--label", CONTAINER_LABEL,
        "--label", f"{SESSION_LABEL}={session}",
        "--label", f"{STARTED_LABEL}={started}",
        "--publish", f"127.0.0.1::{GARAGE_S3_PORT}",
        "--tmpfs", "/var/lib/garage",
        "--volume", f"{config_path}:{GARAGE_CONFIG}:ro",
        image,
    ]


def garage_argv(container: str, *args: str) -> list[str]:
    """`garage ...` inside the container; the image has no shell, only the binary."""
    return ["docker", "exec", container, "/garage", *args]


def garage_setup_argvs(container: str, node: str, key_id: str, secret: str) -> list[list[str]]:
    """In order: give the one node a role, apply that layout, import the key the suite chose, create the bucket and
    let the key use it. Importing a chosen key keeps the secret out of every command's output the fixture reads."""
    return [
        garage_argv(container, "layout", "assign", "--zone", "dc1", "--capacity", "1G", node[:16]),
        garage_argv(container, "layout", "apply", "--version", "1"),
        garage_argv(container, "key", "import", "--yes", "-n", GARAGE_KEY_NAME, key_id, secret),
        garage_argv(container, "bucket", "create", GARAGE_BUCKET),
        garage_argv(container, "bucket", "allow", "--read", "--write", "--owner", GARAGE_BUCKET, "--key", GARAGE_KEY_NAME),
    ]


def parse_node_id(output: str) -> str:
    """The node id `garage node id` printed (`<64 hex>@<address>`). Raises ValueError when there is none."""
    found = [m.group(1) for line in output.splitlines() if (m := _NODE_ID.match(line.strip()))]
    if not found:
        raise ValueError("garage node id printed no node id")
    return found[-1]


def garage_endpoint(port: int, host: str = "127.0.0.1") -> str:
    return f"http://{host}:{port}"


def object_store_provider_text(storage_url: str, endpoint: str, bucket: str = GARAGE_BUCKET) -> str:
    """A provider profile for a shared store: the Postgres, the traces in `bucket` and the object_store block that
    names the credential variables, never their values, as `docs/customize/several-machines.md` lays it out."""
    return "".join([
        f"storage_url: {storage_url}\n",
        f"traces_url: s3://{bucket}/traces\n",
        "object_store:\n",
        f"  endpoint: {endpoint}\n",
        f"  region: {GARAGE_REGION}\n",
        f"  access_key_env: {GARAGE_ACCESS_KEY_ENV}\n",
        f"  secret_key_env: {GARAGE_SECRET_KEY_ENV}\n",
        "  path_style: true\n",
    ])


# --- the harness, offline ------------------------------------------------------------------------------------------

# The roles a team binds for the epic: the base contract's required five, and the two validators the phase graph runs.
SMOKE_SKILLS = {
    "plan": "smoke-skills:plan",
    "build": "smoke-skills:build",
    "review_charter": "smoke-skills:review",
    "work_state_arm": "smoke-skills:work-state",
    "work_item_arm": "smoke-skills:work-item",
    "validate_chunk": "smoke-skills:validate-chunk",
    "validate_phase": "smoke-skills:validate-phase",
}


# The file the scripted build adds, and the probe a target repository's own venv holds (`<repo>/.venv/bin`).
SMOKE_NOTE = "notes/t1.txt"
LANE_PROBE = "smoke-lane-probe"
LANE_PROBE_TEXT = "#!/bin/sh\nexit 0\n"


def smoke_checks(env_out: str) -> tuple[dict[str, str], ...]:
    """The checks the team cartridge runs on every built task, in the worktree the build made.

    `note` passes only once the patch is applied there. `lane-probe` passes only when `<repo>/.venv/bin` is on the
    check's PATH, which is the first prefix `cox route launch` gives the harness (`route.child_env`); a lane's real
    checks (`uv run pytest`, `ruff`) are found the same way. `lane-env` writes the trace dir and PATH the harness was
    given to `env_out`, and fails when the trace dir is unset, so a test can hold both to what cox should have built."""
    return (
        {"name": "note", "cmd": f"grep -qx ok {SMOKE_NOTE}"},
        {"name": "lane-probe", "cmd": f"command -v {LANE_PROBE}"},
        {"name": "lane-env", "cmd": f"printenv AGENT_GRAPHS_TRACE_DIR PATH > {env_out}"},
    )


def lane_env(env: Mapping[str, str], repo: str, trace_dir: str) -> dict[str, str]:
    """`env` as a lane gives it to a harness started by hand: `<repo>/.venv/bin` first on PATH and the trace dir set,
    for the one test that runs the harness's entry point directly rather than through `cox route launch`."""
    return {**env, "PATH": f"{repo}/.venv/bin:{env.get('PATH', '')}", "AGENT_GRAPHS_TRACE_DIR": trace_dir}


def lane_env_lines(text: str) -> tuple[str, str] | None:
    """(trace dir, PATH) from what the `lane-env` check wrote; None when it wrote anything else."""
    lines = text.splitlines()
    return (lines[0], lines[1]) if len(lines) == 2 else None


def team_cartridge_text(team: str, worktree_root: str, checks: Sequence[Mapping[str, str]] = ()) -> str:
    """A team cartridge over `base`: the smoke skills bound, worktrees under `worktree_root`, and `checks` run on each
    built task. JSON is YAML, so the cartridge loader reads it as written."""
    landing: dict[str, object] = {"worktree_root": worktree_root, "checks": [dict(c) for c in checks]}
    return json.dumps({"team": team, "extends": "base", "version": 1, "skills": SMOKE_SKILLS, "landing_areas": landing},
                      indent=2) + "\n"


def skill_plugin_files(skills: Mapping[str, str] = SMOKE_SKILLS) -> dict[str, str]:
    """`{relative path: text}` of the one plugin that holds every skill `skills` binds: its manifest and a SKILL.md
    per skill. The scripted runner never reads a body; the cartridge loader refuses a binding with none."""
    plugins = {name.split(":", 1)[0] for name in skills.values()}
    if len(plugins) != 1:
        raise ValueError(f"the smoke skills name one plugin, not {sorted(plugins)}")
    files = {".claude-plugin/plugin.json": json.dumps({"name": plugins.pop()}) + "\n"}
    for name in skills.values():
        skill = name.split(":", 1)[1]
        files[f"skills/{skill}/SKILL.md"] = f"---\nname: {skill}\ndescription: smoke\n---\n\nThe {skill} skill.\n"
    return files


def new_file_patch(path: str, text: str) -> str:
    """A unified diff that adds `path` holding the one line `text`."""
    return f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+{text}\n"


def scripted_epic_responses(patch: str, files: Sequence[str]) -> dict[str, object]:
    """`--scripted` answers for a one-task epic whose build returns `patch`: every role the run calls, approving."""
    return {
        "plan": {"steps": ["write the file"], "files_expected": list(files), "out_of_scope": []},
        "build": {"patch": patch, "summary": "built", "files_touched": list(files), "commands_run": []},
        "review_charter": {"verdict": "approve", "findings": [], "rationale": "matches the charter"},
        "validate_chunk": {"satisfied": True, "gaps": [], "reasoning": "the description is satisfied"},
        "validate_phase": {"goal_met": True, "partial": False, "missing": [], "quarantine_blocks_dependents": False,
                           "reasoning": "the pieces add up"},
        "work_state_arm": {"applied": True, "detail": "state moved"},
    }


def scripted_provider_profile_text(storage_url: str, scripted: str) -> str:
    """The provider profile of a sandbox that runs graphs offline: the store, the harness's scripted runner replaying
    the answers at `scripted`, an empty tier map (the profile loader requires the block; no model is named), and tool
    use declared, which the gate requires of a runner that edits files. `cox route launch` passes the harness only
    this profile, so this is how a real launch runs offline end to end."""
    return (provider_profile_text(storage_url) + f"runner: scripted\nscripted: {scripted}\ntiers: {{}}\n"
            "capabilities:\n  tool_use: true\n")


# --- GitHub, replayed for the real gh ------------------------------------------------------------------------------

# gh's local-development host: for it gh speaks plain http to `api.github.localhost`, which reaches the replay through
# HTTP_PROXY. A remote on this host is a GitHub remote to gh, and to no forge coxswain picks by `origin`.
GH_HOST = "github.localhost"
GH_SLUG = "ppfenning/coxswain"  # the repository the gh contract tests read: this one, which is public
GH_RECORD_ENV = "COX_SMOKE_GH_RECORD"  # 1: run the gh contract tests against api.github.com and rewrite the recordings
GH_TOKEN_ENV = "COX_SMOKE_GH_TOKEN"  # the token the recorder sends to api.github.com; read only when recording
GH_REPLAY_TOKEN = "smoke-replay"  # what gh itself sends; the replay ignores it and the recorder replaces it
# The request headers the recorder passes on: what selects the API's version and answer format, never a credential.
GH_FORWARDED_HEADERS = frozenset({"accept", "content-type", "x-github-api-version", "graphql-features"})
# The answer headers gh reads: the body's type, and the next page of a paginated list.
_GH_KEPT_HEADERS = ("Content-Type", "Link")
_GH_OPERATION = re.compile(r"^\s*(query|mutation)\b\s*(\w*)")
_GH_UPSTREAM = "https://api.github.com/"


def gh_path(target: str) -> str:
    """The path and query of a request line's target: a proxied request names the whole URL, a direct one the path."""
    parts = urlsplit(target)
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


def _graphql_operation(body: bytes) -> tuple[str, str, str] | None:
    """(`query` or `mutation`, operation name, canonical variables) of a GraphQL request body; None when unreadable."""
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return None
    match = _GH_OPERATION.match(str(data.get("query") or "")) if isinstance(data, dict) else None
    if match is None:
        return None
    return match.group(1), match.group(2), json.dumps(data.get("variables") or {}, sort_keys=True, separators=(",", ":"))


def gh_request_key(method: str, target: str, body: bytes) -> str:
    """What a recorded answer is filed under: the method and the path with its query parameters sorted, or for
    GraphQL the operation's kind, name and variables. Not the query text: a gh release that reorders a selection keeps
    its key, and one that asks for a field the recording lacks reads it as missing, which the test then fails on."""
    parts = urlsplit(gh_path(target))
    if method == "POST" and parts.path == "/graphql":
        operation = _graphql_operation(body)
        if operation is not None:
            return "POST /graphql " + " ".join(operation)
    query = "&".join(f"{k}={v}" for k, v in sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return f"{method} {parts.path}" + (f"?{query}" if query else "")


def gh_is_write(method: str, target: str, body: bytes) -> bool:
    """True for anything but a read: a GET or HEAD, or a GraphQL query (a mutation is a write)."""
    if method in ("GET", "HEAD"):
        return False
    if method == "POST" and urlsplit(gh_path(target)).path == "/graphql":
        operation = _graphql_operation(body)
        return operation is None or operation[0] != "query"
    return True


_GH_BRANCH_DELETE = re.compile(r"^/repos/([^/]+)/([^/]+)/git/refs/heads/(.+)$")


def gh_absent_branch_probe(method: str, target: str) -> str | None:
    """For a branch deletion, the GET that shows whether the branch exists; None for any other request. The recorder
    forwards the deletion only when that GET answers 404: deleting a branch that is gone changes nothing on GitHub."""
    match = _GH_BRANCH_DELETE.match(urlsplit(gh_path(target)).path) if method == "DELETE" else None
    return None if match is None else f"/repos/{match.group(1)}/{match.group(2)}/git/ref/heads/{match.group(3)}"


def gh_recording_name(test_name: str) -> str:
    """The recording file of one test: its name, parameters included, made safe for a file name."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", test_name).strip("-") + ".json"


def gh_scrub(value: object) -> object:
    """`value` with every git identity (an object holding both `name` and `email`) replaced by the sandbox's own."""
    if isinstance(value, list):
        return [gh_scrub(v) for v in value]
    if not isinstance(value, dict):
        return value
    scrubbed = {k: gh_scrub(v) for k, v in value.items()}
    if "name" in scrubbed and "email" in scrubbed:
        scrubbed.update(name="Smoke Test", email="smoke@example.invalid")
    return scrubbed


def gh_exchange(key: str, status: int, headers: Mapping[str, str], text: str) -> dict:
    """One recorded answer: the headers gh reads (a Link's pages pointed at the replay host) and the body, scrubbed
    when it is JSON."""
    kept = {name: value for name in _GH_KEPT_HEADERS for k, value in headers.items() if k.lower() == name.lower()}
    if "Link" in kept:
        kept["Link"] = kept["Link"].replace(_GH_UPSTREAM, f"http://api.{GH_HOST}/")
    try:
        body = json.dumps(gh_scrub(json.loads(text)), separators=(",", ":"))
    except ValueError:
        body = text
    return {"key": key, "status": status, "headers": kept, "body": body}


def gh_exchange_queues(exchanges: Sequence[Mapping]) -> dict[str, list[Mapping]]:
    """The recorded answers by key, in the order they were recorded."""
    queues: dict[str, list[Mapping]] = {}
    for exchange in exchanges:
        queues.setdefault(str(exchange["key"]), []).append(exchange)
    return queues


def gh_env(proxy: str, config_dir: str) -> dict[str, str]:
    """What sends a sandboxed gh to the replay or the recorder at `proxy`: the local host, the proxy for http and
    https alike (so a request gh made to any other host also reaches the replay, which has no answer for it, and never
    the network), a config folder of its own, a placeholder token, and no prompt, update check or telemetry.

    Telemetry is off by gh's own documented switches (`gh help environment`): `GH_TELEMETRY=0`, which takes precedence,
    and `DO_NOT_TRACK=1`. gh posts a `command_invocation` event to https://cafe.github.com; through HTTPS_PROXY that
    arrived at the replay as `CONNECT cafe.github.com:443`, a miss, and failed a test on CI (run 44433933282). The
    feature is switched off rather than its host allowed, so any other stray request still fails the test."""
    return {"GH_HOST": GH_HOST, "HTTP_PROXY": proxy, "http_proxy": proxy, "HTTPS_PROXY": proxy, "https_proxy": proxy,
            "NO_PROXY": "", "no_proxy": "",
            "GH_TOKEN": GH_REPLAY_TOKEN, "GH_CONFIG_DIR": config_dir, "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1", "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1", "GH_SPINNER_DISABLED": "1",
            "GH_TELEMETRY": "0", "DO_NOT_TRACK": "1"}


def gh_remote_url(slug: str) -> str:
    """The origin URL of a clone of `slug` on the replay's host."""
    return f"https://{GH_HOST}/{slug}.git"
