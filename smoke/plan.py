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
    return ["docker", "port", container, "5432/tcp"]


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
# on the host `FORGEJO_BASE_URL` names. Bump the digest on purpose, never by pulling the tag.
FORGEJO_IMAGE = "codeberg.org/forgejo/forgejo:11@sha256:946243edbab116d5bb78b73ea68af6f3d69229ba1b1ed958dd82c3481167f3e0"
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
    if docker_path is None:
        return unavailable("no docker on PATH for the sandbox forge", required=required)
    if not docker_ok:
        reason = "docker is on PATH but its daemon did not answer, so the sandbox forge cannot start"
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
    """The paths of `git ls-files -z --cached --others --exclude-standard`: the tree as committed plus what is new and
    not ignored, which is what a checkout of this change would hold. Sorted, empty names dropped."""
    return sorted({path for path in listing.split("\0") if path})


def closed_port_url(url: str, port: int) -> str:
    """`url` with its port replaced by `port`, a loopback port nothing listens on: a store the doctor cannot reach."""
    parts = urlsplit(url)
    credentials, _, _host = parts.netloc.rpartition("@")
    netloc = f"{credentials}@127.0.0.1:{port}" if credentials else f"127.0.0.1:{port}"
    return parts._replace(netloc=netloc).geturl()


def branch_tip(status: int, body: object) -> str | None:
    """The commit id of a `GET /repos/{owner}/{repo}/branches/{branch}` answer; None for a missing branch (404) or a
    body without one."""
    commit = body.get("commit") if status == 200 and isinstance(body, dict) else None
    sha = commit.get("id") if isinstance(commit, dict) else None
    return sha if isinstance(sha, str) and sha else None


def pull_summary(pull: object) -> dict:
    """What a smoke test checks of a `GET /repos/{owner}/{repo}/pulls/{n}` body: state, merged, head branch and commit,
    and base branch. A body that is not a pull request is `{}`."""
    if not isinstance(pull, dict):
        return {}
    head, base = pull.get("head") or {}, pull.get("base") or {}
    return {"state": pull.get("state"), "merged": pull.get("merged") is True, "head": head.get("ref"),
            "sha": head.get("sha"), "base": base.get("ref")}
