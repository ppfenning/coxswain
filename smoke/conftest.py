"""The smoke suite's shell: real processes, a real Postgres, real git, and `cox` installed from this tree.

Nothing here is mocked. Every decision is made by `smoke/plan.py`; this file gathers its inputs and runs what it
returns. The suite never opens a store that is not on a loopback address, and every `cox` it runs sees a HOME, XDG
roots, profile and workspace inside a temporary directory, so nothing reads or writes a real coxswain home.

Run it from the repository root: `pytest -q -rs smoke`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import quote

import pytest

from smoke import plan

ROOT = Path(__file__).resolve().parents[1]
# One reader of a Forgejo branch answer, shared with the code under test: the forge's own parser, imported from this
# tree (not from whatever checkout the runner's venv installed), since the fixtures poll the same endpoint it does.
sys.path.insert(0, str(ROOT / "src" / "tools"))
from agent_tools.forge_forgejo import parse_branch_tip  # noqa: E402

SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"
READY_TIMEOUT_S = 120
# Every command the fixtures run is bounded: a hung git, docker or uv fails the test that waited on it.
COMMAND_TIMEOUT_S = 600
# This session's id, on the label of the container it starts: `docker ps --filter label=coxswain-smoke.session=<id>`
# finds it. Printed in the report header. The suite runs serial, so a run is one session.
SESSION = uuid.uuid4().hex[:12]


def pytest_report_header() -> str:
    return f"smoke session: {SESSION} (container label {plan.SESSION_LABEL}={SESSION})"


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, **{"timeout": COMMAND_TIMEOUT_S, **kwargs})


def _must(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    done = _run(argv, **kwargs)
    if done.returncode != 0:
        raise RuntimeError(f"{argv[:4]} exited {done.returncode}: {done.stderr.strip() or done.stdout.strip()}")
    return done


def _unavailable(decision: plan.Skip | plan.Fail) -> None:
    if isinstance(decision, plan.Fail):
        pytest.fail(decision.reason, pytrace=False)
    pytest.skip(decision.reason)


def _required() -> bool:
    return plan.is_required(os.environ.get(plan.REQUIRED_ENV))


# --- the install ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Install:
    """`cox` installed (not editable) from this tree into a venv of its own. `root` is the harness_dir a profile names:
    `install_root.harness_python` finds `root/.venv/bin/python`, so every child the harness starts runs this install."""

    root: Path
    python: Path
    bin: Path
    site: Path
    env: dict[str, str]  # sandboxed, with a home of the install's own: what the fixtures' own harness calls run with

    @property
    def cox(self) -> Path:
        return self.bin / "cox"


def _files(base: Path, folder: str, pattern: str) -> dict[str, str]:
    """`<name>: sha256` for each file in `base / folder` matching `pattern`, named relative to the folder."""
    found = [p.relative_to(base / folder).as_posix() for p in (base / folder).glob(pattern) if p.is_file()]
    names = plan.python_sources(found) if pattern == "*.py" else sorted(found)
    return {name: hashlib.sha256((base / folder / name).read_bytes()).hexdigest() for name in names}


def _install_drift(site: Path) -> list[str]:
    """Every module and every `package-data` file, compared between the tree and the install by installed path."""
    tool = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]
    dirs = plan.package_dirs(tool["packages"], tool["package-dir"])
    globs = [(src, inst, "*.py") for src, inst in dirs.values()] + plan.package_data(tool.get("package-data", {}), dirs)
    tree, installed = {}, {}
    for src, inst, pattern in globs:
        tree.update({f"{inst}/{name}": digest for name, digest in _files(ROOT, src, pattern).items()})
        installed.update({f"{inst}/{name}": digest for name, digest in _files(site, inst, pattern).items()})
    return plan.install_drift(tree, installed)


def _clean_env(home: Path, bins: list[str]) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    return plan.sandbox_env(os.environ, home=str(home), bin_dirs=bins, system_path=SYSTEM_PATH)


@pytest.fixture(scope="session")
def install(tmp_path_factory: pytest.TempPathFactory) -> Install:
    uv = shutil.which("uv")
    if uv is None:
        _unavailable(plan.unavailable("no uv on PATH to install cox from this tree", required=_required()))
    root = tmp_path_factory.mktemp("install")
    venv = root / ".venv"
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    _must([uv, "venv", "--quiet", "--python", version, str(venv)])
    python = venv / "bin" / "python"
    # --reinstall-package: uv caches a local build by its pyproject's mtime, so without it an edited .py file would
    # install the previous build. The drift check below proves the install is this tree either way.
    _must([uv, "pip", "install", "--quiet", "--python", str(python), "--reinstall-package", "coxswain", f"{ROOT}[postgres]"])
    env = _clean_env(root / "home", [str(venv / "bin")])
    site = Path(_must([str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"], env=env).stdout.strip())
    drift = _install_drift(site)
    if drift:
        pytest.fail(f"the install is not this tree: {drift[:5]}", pytrace=False)
    return Install(root=root, python=python, bin=venv / "bin", site=site, env=env)


# --- Postgres ------------------------------------------------------------------------------------------------------


def _docker_ok(docker: str | None) -> bool:
    return docker is not None and _run([docker, "info", "--format", "{{.ServerVersion}}"], timeout=30).returncode == 0


def _reap_stale() -> None:
    """Remove smoke containers a killed session left behind: only those whose start label is older than
    `plan.STALE_AFTER_S`, so a session that is still running keeps its container."""
    listing = _run(plan.docker_list_argv())
    stale = plan.stale_containers(listing.stdout, int(time.time())) if listing.returncode == 0 else []
    if stale:
        _run(plan.docker_remove_argv(*stale))


def _ready_port(container: str) -> int:
    deadline = time.monotonic() + READY_TIMEOUT_S
    while _run(plan.docker_ready_argv(container)).returncode != 0:
        if time.monotonic() > deadline:
            raise RuntimeError(f"Postgres in {container[:12]} was not ready after {READY_TIMEOUT_S}s")
        time.sleep(0.25)
    return plan.parse_published_port(_must(plan.docker_port_argv(container)).stdout)


@pytest.fixture(scope="session")
def postgres() -> Iterator[str]:
    """The URL of the session's Postgres server: the one `COX_SMOKE_POSTGRES_URL` names, else a container started here
    and removed at the end of the session."""
    docker = shutil.which("docker")
    url = os.environ.get(plan.URL_ENV)
    broken_ok = plan.is_required(os.environ.get(plan.BROKEN_DOCKER_OK_ENV))
    docker_ok = url is None and _docker_ok(docker)
    decision = plan.postgres_source(url, docker, docker_ok, required=_required(), broken_docker_ok=broken_ok)
    if isinstance(decision, plan.Skip | plan.Fail):
        _unavailable(decision)
    if isinstance(decision, plan.UseUrl):
        yield decision.url
        return
    _reap_stale()
    container = _must(plan.docker_run_argv(SESSION, int(time.time()))).stdout.strip()
    try:  # from the moment the container exists, every way out of this fixture removes it
        yield plan.postgres_url(_ready_port(container))
    finally:
        _run(plan.docker_remove_argv(container))


_ADMIN = """import sys, psycopg
url, verb, name = sys.argv[1:4]
with psycopg.connect(url, autocommit=True) as conn:
    conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    if verb == "create":
        conn.execute(f'CREATE DATABASE "{name}"')
"""


@pytest.fixture
def store_url(postgres: str, install: Install, request: pytest.FixtureRequest) -> Iterator[str]:
    """A fresh database for one test, at the newest schema. The harness's own store CLI opens it first, which is
    what applies the migrations (`harness.store_migrate.open_store`)."""
    name = plan.database_name(request.node.nodeid)
    # install.env, not the caller's: no PYTHONPATH, no PG* variables and no real HOME reach these calls
    admin = [str(install.python), "-c", _ADMIN, postgres]
    _must([*admin, "create", name], env=install.env, cwd=install.root)
    url = plan.with_database(postgres, name)
    _must([str(install.python), "-m", "harness.store_cli", "--store-url", url, "host", "list"], env=install.env, cwd=install.root)
    try:
        yield url
    finally:
        _run([*admin, "drop", name], env=install.env, cwd=install.root)


# --- the sandbox ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Sandbox:
    """One person's machine, in a temporary directory: a home with a routing profile, a workspace, and a store."""

    home: Path
    workspace: Path
    env: dict[str, str]
    install: Install
    store_url: str

    def cox(self, *args: str, cwd: Path | None = None, timeout: float = 120) -> subprocess.CompletedProcess[str]:
        """Run the installed `cox` the way a person would, from `cwd` (default the sandbox home)."""
        return _run([str(self.install.cox), *args], env=self.env, cwd=cwd or self.home, timeout=timeout)

    def harness(self, module: str, *args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
        """Run `python -m <module>` under the install's interpreter, the one `cox` itself starts for the harness."""
        return _run([str(self.install.python), "-m", module, *args], env=self.env, cwd=self.home, timeout=timeout)

    def python_json(self, code: str) -> object:
        """Run `code` under the install's interpreter and parse the JSON it prints: what the installed packages say."""
        done = _run([str(self.install.python), "-c", code], env=self.env, cwd=self.home, timeout=60)
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout)

    def git(self, repo: Path, *args: str) -> str:
        return _must(["git", "-C", str(repo), *args], env=self.env).stdout.strip()

    def write(self, relative: str, text: str) -> Path:
        """Write `text` to `relative` under the workspace."""
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def store_rows(self) -> list[dict]:
        """Every work_items row, read through the harness's own queue CLI."""
        done = self.harness("harness.store_queue", "read", self.store_url)
        assert done.returncode == 0, done.stderr
        return [json.loads(line) for line in done.stdout.splitlines() if line.strip()]

    def store_row(self, initiative: str, task_id: str) -> dict:
        return next(r for r in self.store_rows() if (r["initiative"], r["task_id"]) == (initiative, task_id))


@pytest.fixture
def sandbox(tmp_path: Path, install: Install, store_url: str) -> Sandbox:
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    config = home / ".config" / "agent-tools"
    config.mkdir(parents=True)
    (workspace / "runs").mkdir(parents=True)
    (workspace / "work").mkdir()
    provider = config / "provider.yaml"
    provider.write_text(plan.provider_profile_text(store_url), encoding="utf-8")
    (config / "profile.yaml").write_text(
        plan.routing_profile_text(team="smoke", workspace=str(workspace), harness_dir=str(install.root), provider_profile=str(provider)),
        encoding="utf-8",
    )
    (home / ".gitconfig").write_text(plan.gitconfig_text(), encoding="utf-8")
    git = shutil.which("git")
    if git is None:
        _unavailable(plan.unavailable("no git on PATH", required=_required()))
    # git's own folder too, for a host whose git is not under SYSTEM_PATH (a Homebrew prefix, say)
    bins = list(dict.fromkeys([str(install.bin), str(Path(git).parent)]))
    env = plan.sandbox_env(os.environ, home=str(home), bin_dirs=bins, system_path=SYSTEM_PATH)
    return Sandbox(home=home, workspace=workspace, env=env, install=install, store_url=store_url)


@dataclass(frozen=True)
class Repo:
    path: Path
    origin: Path


@pytest.fixture
def repo(sandbox: Sandbox, tmp_path: Path) -> Repo:
    """A clone at `~/src/app` of a bare origin, with one commit on main pushed."""
    origin, path = tmp_path / "origin.git", sandbox.home / "src" / "app"
    _must(["git", "init", "--quiet", "--bare", str(origin)], env=sandbox.env)
    _must(["git", "clone", "--quiet", str(origin), str(path)], env=sandbox.env)
    (path / "README.md").write_text("app\n", encoding="utf-8")
    sandbox.git(path, "add", "README.md")
    sandbox.git(path, "commit", "--quiet", "-m", "init")
    sandbox.git(path, "push", "--quiet", "origin", "main")
    # A clone of an empty origin has no origin/HEAD; a clone of a real repository does, and `cox runs recover` reads it.
    sandbox.git(path, "remote", "set-head", "origin", "main")
    return Repo(path=path, origin=origin)


# --- the sandbox forge ---------------------------------------------------------------------------------------------

API_TIMEOUT_S = 15


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", f"{plan.FORGE_MARK}: needs the sandbox forge, a Forgejo container (docs/smoke.md)")


def _free_port() -> int:
    """A loopback port nothing listens on now. Forgejo's ROOT_URL must name its published port, so the port is chosen
    before the container starts; a process that takes it in between fails the start, loudly."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@dataclass(frozen=True)
class Forge:
    """The session's Forgejo: its address, its one admin user and that user's token."""

    base_url: str
    user: str
    token: str = field(repr=False)

    def api(self, method: str, path: str, body: object | None = None, *, raw: bool = False) -> tuple[int, object | None]:
        """One REST call, as `(status, json)`, or `(status, text)` when `raw`; an HTTP error status is a response,
        not an exception."""
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Authorization": f"token {self.token}", "Accept": "application/json",
                   **({"Content-Type": "application/json"} if data is not None else {})}
        request = urllib.request.Request(f"{self.base_url}/api/v1{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=API_TIMEOUT_S) as response:
                status, reply = response.status, response.read()
        except urllib.error.HTTPError as exc:
            with exc:
                status, reply = exc.code, exc.read()
        if raw:
            return status, reply.decode("utf-8", errors="replace")
        return status, (json.loads(reply) if reply else None)

    def must(self, method: str, path: str, body: object | None = None) -> object | None:
        status, data = self.api(method, path, body)
        if not 200 <= status < 300:
            raise RuntimeError(f"{method} {path} answered {status}: {data}")
        return data

    @property
    def env(self) -> dict[str, str]:
        return plan.forge_env(self.base_url, self.token)


def _forge_ready(base_url: str) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_S
    while True:
        try:
            with urllib.request.urlopen(f"{base_url}/api/healthz", timeout=5) as response:
                if response.status == 200:
                    return
        except OSError:  # URLError and a refused or reset connection, while the server starts
            pass
        if time.monotonic() > deadline:
            raise RuntimeError(f"Forgejo at {base_url} was not ready after {READY_TIMEOUT_S}s")
        time.sleep(0.25)


def _pull(image: str) -> None:
    """`docker pull`, tried `plan.PULL_ATTEMPTS` times with a short pause, so one registry error does not fail the run."""
    for attempt in range(1, plan.PULL_ATTEMPTS + 1):
        done = _run(plan.docker_pull_argv(image))
        if done.returncode == 0:
            return
        if attempt == plan.PULL_ATTEMPTS:
            raise RuntimeError(f"docker pull {image} failed {attempt} times: {done.stderr.strip() or done.stdout.strip()}")
        time.sleep(5 * attempt)


@pytest.fixture(scope="session")
def forge() -> Iterator[Forge]:
    """A Forgejo container on a loopback port, with one admin user and a token, removed at the end of the session."""
    docker = shutil.which("docker")
    broken_ok = plan.is_required(os.environ.get(plan.BROKEN_DOCKER_OK_ENV))
    decision = plan.forge_source(docker, _docker_ok(docker), required=_required(), broken_docker_ok=broken_ok)
    if isinstance(decision, plan.Skip | plan.Fail):
        _unavailable(decision)
    _reap_stale()
    _pull(plan.FORGEJO_IMAGE)
    port = _free_port()
    container = _must(plan.forgejo_run_argv(SESSION, int(time.time()), port)).stdout.strip()
    try:  # from the moment the container exists, every way out of this fixture removes it
        base_url = plan.forgejo_base_url(port)
        _forge_ready(base_url)
        _must(plan.forgejo_user_argv(container, uuid.uuid4().hex))
        token = plan.parse_token(_must(plan.forgejo_token_argv(container, f"smoke-{SESSION}")).stdout)
        yield Forge(base_url=base_url, user=plan.FORGEJO_USER, token=token)
    finally:
        _run(plan.docker_remove_argv(container))


@dataclass(frozen=True)
class ForgeRepo:
    """A repository on the sandbox forge, and its clone at `~/src/app` in the sandbox."""

    forge: Forge
    owner: str
    name: str
    path: Path

    def api(self, method: str, tail: str = "", body: object | None = None) -> object | None:
        return self.forge.must(method, f"/repos/{self.owner}/{self.name}{tail}", body)

    def pull(self, number: int) -> dict:
        """Pull request `number`: state, merged, head branch and commit, base branch."""
        return plan.pull_summary(self.api("GET", f"/pulls/{number}"))

    def report(self, sha: str, state: str, context: str = "ci") -> None:
        """Report a commit status, as a CI job would."""
        self.api("POST", f"/statuses/{sha}", plan.commit_status_body(state, context))

    def branch_tip(self, branch: str) -> str | None:
        """The commit `branch` points at on the forge, or None when the forge has no such branch."""
        status, body = self.forge.api("GET", f"/repos/{self.owner}/{self.name}/branches/{quote(branch, safe='')}")
        return parse_branch_tip(status, body)

    def file_text(self, ref: str, path: str) -> str | None:
        """`path` at `ref` on the forge, or None when it is not there."""
        status, body = self.forge.api("GET", f"/repos/{self.owner}/{self.name}/raw/{quote(path)}?ref={quote(ref, safe='')}", raw=True)
        return body if status == 200 and isinstance(body, str) else None


@pytest.fixture
def forge_sandbox(sandbox: Sandbox, forge: Forge) -> Sandbox:
    """The sandbox with the forge's address and token in its environment and `forge: auto` in its profile, so `cox`
    lands a repository whose origin is on the forge through `forge_forgejo`, as a person's machine would."""
    config = sandbox.home / ".config" / "agent-tools"
    (config / "profile.yaml").write_text(
        plan.routing_profile_text(team="smoke", workspace=str(sandbox.workspace), harness_dir=str(sandbox.install.root),
                                  provider_profile=str(config / "provider.yaml"), extra={"forge": "auto"}),
        encoding="utf-8",
    )
    return replace(sandbox, env={**sandbox.env, **forge.env})


def _git(env: dict[str, str], repo: Path, *args: str) -> str:
    return _must(["git", "-C", str(repo), *args], env=env).stdout.strip()


def _publish(forge: Forge, env: dict[str, str], name: str, path: Path) -> None:
    """Create repository `name` on the forge, push main of the repository at `path` to it, and wait until the forge
    shows it: Forgejo records a pushed branch from a queue after the push returns."""
    forge.must("POST", "/user/repos", {"name": name, "default_branch": "main", "private": False})
    _git(env, path, "remote", "add", "origin", plan.forge_remote_url(forge.base_url, forge.user, name, forge.token))
    _git(env, path, "push", "--quiet", "--set-upstream", "origin", "main")
    _git(env, path, "remote", "set-head", "origin", "main")
    sha, tail = _git(env, path, "rev-parse", "main"), f"/repos/{forge.user}/{name}/branches/main"
    deadline = time.monotonic() + READY_TIMEOUT_S
    while parse_branch_tip(*forge.api("GET", tail)) != sha:
        if time.monotonic() > deadline:
            raise RuntimeError(f"the forge did not show main of {name} at {sha[:12]} after {READY_TIMEOUT_S}s")
        time.sleep(0.1)


def _forge_clone(sandbox: Sandbox, forge: Forge, name: str, path: Path) -> None:
    """Create repository `name` on the forge and push a first commit on main to it from a new clone at `path`."""
    _must(["git", "init", "--quiet", str(path)], env=sandbox.env)
    (path / "README.md").write_text("app\n", encoding="utf-8")
    _git(sandbox.env, path, "add", "README.md")
    _git(sandbox.env, path, "commit", "--quiet", "-m", "init")
    _publish(forge, sandbox.env, name, path)


@pytest.fixture
def forge_repo(forge_sandbox: Sandbox, forge: Forge, request: pytest.FixtureRequest) -> ForgeRepo:
    """A new repository on the forge with one commit on main, cloned at `~/src/app` with the forge as its origin."""
    name = plan.forge_repo_name(request.node.nodeid)
    path = forge_sandbox.home / "src" / "app"
    path.parent.mkdir(parents=True, exist_ok=True)
    _forge_clone(forge_sandbox, forge, name, path)
    return ForgeRepo(forge=forge, owner=forge.user, name=name, path=path)


# --- an edge machine -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EdgeOrigin:
    """This tree on the forge at two commits of main, both reported green: `base`, then `newer` on top of it."""

    name: str
    base: str
    newer: str


def _copy_tree(target: Path) -> None:
    """This tree's tracked files, as a clone of this change would hold them, copied into `target`."""
    listing = _must(["git", "-C", str(ROOT), "ls-files", "-z"]).stdout
    for name in plan.tree_files(listing):
        source, dest = ROOT / name, target / name
        if not source.is_symlink() and not source.is_file():
            continue  # deleted in the working tree, or a submodule
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest, follow_symlinks=False)


@pytest.fixture(scope="module")
def edge_origin(forge: Forge, tmp_path_factory: pytest.TempPathFactory) -> EdgeOrigin:
    """A coxswain repository on the forge: this tree as one commit, and one commit more that changes one file. CI is
    reported green on both, through commit statuses, as Forgejo Actions would."""
    work = tmp_path_factory.mktemp("edge-origin")
    git_bin = shutil.which("git")
    if git_bin is None:
        _unavailable(plan.unavailable("no git on PATH", required=_required()))
    env = _clean_env(work / "home", [str(Path(git_bin).parent)])
    (work / "home" / ".gitconfig").write_text(plan.gitconfig_text(), encoding="utf-8")
    tree = work / "coxswain"
    _copy_tree(tree)
    _must(["git", "init", "--quiet", str(tree)], env=env)
    _git(env, tree, "add", "--all")
    _git(env, tree, "commit", "--quiet", "-m", "smoke: this tree")
    base = _git(env, tree, "rev-parse", "HEAD")
    (tree / "smoke-edge.txt").write_text("one commit newer\n", encoding="utf-8")
    _git(env, tree, "add", "smoke-edge.txt")
    _git(env, tree, "commit", "--quiet", "-m", "smoke: one commit newer")
    newer = _git(env, tree, "rev-parse", "HEAD")
    name = plan.forge_repo_name(f"edge-{SESSION}")
    _publish(forge, env, name, tree)
    for sha in (base, newer):
        forge.must("POST", f"/repos/{forge.user}/{name}/statuses/{sha}", plan.commit_status_body("success", "ci"))
    return EdgeOrigin(name=name, base=base, newer=newer)


@pytest.fixture(scope="session")
def uv_shared() -> dict[str, str]:
    """The caller's uv cache and interpreter folders, which a sandboxed uv reads through (`plan.uv_env`)."""
    uv = shutil.which("uv")
    if uv is None:
        _unavailable(plan.unavailable("no uv on PATH", required=_required()))
    return plan.uv_env(_must([uv, "cache", "dir"]).stdout.strip(), _must([uv, "python", "dir"]).stdout.strip())


@dataclass(frozen=True)
class EdgeMachine:
    """A machine on the edge channel, installed the way `cox setup install` lays a home out: the checkout at
    `<data>/coxswain/installs/coxswain`, cloned from the forge and at `origin.base`, its venv synced, and `cox` a uv
    tool editable from it, first on PATH. `cox upgrade` here is that checkout's own `cox`."""

    sandbox: Sandbox
    checkout: Path
    origin: EdgeOrigin

    @property
    def record(self) -> Path:
        return self.checkout.parent / "edge-upgrade.json"

    def cox(self, *args: str, timeout: float = 900) -> subprocess.CompletedProcess[str]:
        return _run([str(self.sandbox.home / ".local" / "bin" / "cox"), *args], env=self.sandbox.env,
                    cwd=self.sandbox.home, timeout=timeout)

    def head(self) -> str:
        return _git(self.sandbox.env, self.checkout, "rev-parse", "HEAD")


@pytest.fixture
def edge_machine(forge_sandbox: Sandbox, forge: Forge, edge_origin: EdgeOrigin, uv_shared: dict[str, str]) -> EdgeMachine:
    home = forge_sandbox.home
    # The uv tool's bin first, so `cox` is the checkout's own, and uv's folder, since the upgrade runs uv as a machine would
    uv_bin = str(Path(shutil.which("uv") or "uv").parent)
    env = {**forge_sandbox.env, **uv_shared, "PATH": f"{home / '.local' / 'bin'}:{forge_sandbox.env['PATH']}:{uv_bin}"}
    sandbox = replace(forge_sandbox, env=env)
    checkout = home / ".local" / "share" / "coxswain" / "installs" / "coxswain"
    checkout.parent.mkdir(parents=True)
    (sandbox.workspace / "intake").mkdir()  # the doctor's workspace row, which an upgrade requires, wants it
    origin = plan.forge_remote_url(forge.base_url, forge.user, edge_origin.name, forge.token)
    _must(["git", "clone", "--quiet", origin, str(checkout)], env=env)
    _git(env, checkout, "reset", "--quiet", "--hard", edge_origin.base)
    # What `cox setup install` runs to lay the checkout down, asked of the installed code rather than restated here.
    sync = sandbox.python_json("import json; from agent_tools import setup_install as s; print(json.dumps(list(s.SYNC_ARGV)))")
    _must([*sync], env=env, cwd=checkout)
    _must(["uv", "tool", "install", "--force", "--quiet", "--editable", f"{checkout}[postgres]"], env=env, cwd=home)
    config = home / ".config" / "agent-tools"
    (config / "profile.yaml").write_text(
        plan.routing_profile_text(team="smoke", workspace=str(sandbox.workspace), harness_dir=str(checkout / "src" / "graphs"),
                                  provider_profile=str(config / "provider.yaml"),
                                  extra={"forge": "auto", "upgrade_channel": "edge"}),
        encoding="utf-8",
    )
    return EdgeMachine(sandbox=sandbox, checkout=checkout, origin=edge_origin)
