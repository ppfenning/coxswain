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
import re
import secrets
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

from smoke import gh_replay, plan

ROOT = Path(__file__).resolve().parents[1]
# This tree's source roots, ahead of the venv, in src/tools/pytest.ini's order: an editable install in the runner's venv
# may point at an older checkout, and `agent_tools` imports `core` and `harness`, so all three must come from here.
# Subprocesses need no PYTHONPATH: every child runs the `install` venv's python, built from this tree, with install.env.
sys.path[:0] = [str(ROOT / "src" / name) for name in ("tools", "graphs", "cartridges")]
# One reader of a Forgejo branch answer, shared with the code under test: the forge's own parser, imported from this
# tree (not from whatever checkout the runner's venv installed), since the fixtures poll the same endpoint it does.
from agent_tools.forge_forgejo import parse_branch_tip  # noqa: E402

# The harness backs a store up with `pg_dump` before it migrates one (`harness.store_migrate`), and a fresh smoke database
# always migrates. Linux has it in /usr/bin; Homebrew puts it under /opt/homebrew, off the minimal PATH, so the folder
# the caller's PATH finds it in is added after the system's, and nothing else from that PATH comes with it.
PG_DUMP = shutil.which("pg_dump")
SYSTEM_PATH = plan.system_path_with("/usr/local/bin:/usr/bin:/bin", str(Path(PG_DUMP).parent) if PG_DUMP else None)
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
    """This tree with `[postgres,traces,lake]`: one install for the whole session. The lake extra is what `cox lake`
    needs (test_lake.py); a second install for it alone cost about a minute of CI on a cold cache."""
    return _install(tmp_path_factory, "install", "postgres,traces,lake")


def _install(tmp_path_factory: pytest.TempPathFactory, name: str, extras: str) -> Install:
    uv = shutil.which("uv")
    if uv is None:
        _unavailable(plan.unavailable("no uv on PATH to install cox from this tree", required=_required()))
    root = tmp_path_factory.mktemp(name)
    venv = root / ".venv"
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    _must([uv, "venv", "--quiet", "--python", version, str(venv)])
    python = venv / "bin" / "python"
    # --reinstall-package: uv caches a local build by its pyproject's mtime, so without it an edited .py file would
    # install the previous build. The drift check below proves the install is this tree either way.
    _must([uv, "pip", "install", "--quiet", "--python", str(python), "--reinstall-package", "coxswain", f"{ROOT}[{extras}]"])
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
    _pull(plan.POSTGRES_IMAGE)  # before the run, which would otherwise pull a missing image itself, with no retry
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
    if PG_DUMP is None:
        pytest.fail("pg_dump is not on PATH: the harness backs up every store it migrates with pg_dump, and each smoke "
                    "database is migrated when it opens. Install the PostgreSQL client tools and put pg_dump on PATH.",
                    pytrace=False)
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

    def python_json(self, code: str, cwd: Path | None = None) -> object:
        """Run `code` under the install's interpreter, from `cwd` (default the sandbox home), and parse the JSON it
        prints: what the installed packages say, or do."""
        done = _run([str(self.install.python), "-c", code], env=self.env, cwd=cwd or self.home, timeout=120)
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


# --- the harness, launched -----------------------------------------------------------------------------------------

TEAM = "smoke"


@dataclass(frozen=True)
class Launchable:
    """The sandbox set up so `cox route launch` can start the harness and run it offline: a team cartridge over the
    tree's `base`, a profile naming it, `shell.py` beside the install's venv as a checkout's `src/graphs` holds it,
    and a provider profile whose runner is the harness's scripted one (`plan.scripted_provider_profile_text`)."""

    sandbox: Sandbox
    cartridges: Path
    skills: Path
    worktrees: Path

    def harness_flags(self) -> list[str]:
        """The team, cartridge, skills, profile and workspace flags `cox route launch` passes the harness."""
        config = self.sandbox.home / ".config" / "agent-tools"
        return ["--team", TEAM, "--cartridges-dir", str(self.cartridges), "--skills-root", str(self.skills),
                "--provider-profile", str(config / "provider.yaml"), "--runs-dir", str(self.sandbox.workspace / "runs"),
                "--assume", "a", "--workdir", str(self.sandbox.workspace)]

    @property
    def answers(self) -> Path:
        """The canned answers the provider profile's scripted runner replays (`plan.scripted_provider_profile_text`)."""
        return self.sandbox.home / "scripted.json"

    def scripted(self, responses: object) -> Path:
        """Write `responses` as the answers the scripted runner replays, for a launch and for `--scripted` alike."""
        path = self.answers
        path.write_text(json.dumps(responses), encoding="utf-8")
        return path

    def lifecycle(self, repo: Path, run_id: str) -> subprocess.CompletedProcess[str]:
        """One scripted lifecycle run that builds and checks `plan.SMOKE_NOTE` in `repo`, recorded in the store."""
        responses = plan.scripted_epic_responses(plan.new_file_patch(plan.SMOKE_NOTE, "ok"), [plan.SMOKE_NOTE])
        argv = [str(self.sandbox.install.python), str(self.sandbox.install.root / "shell.py"), "lifecycle",
                *self.harness_flags(), "--run-id", run_id, "--ticket", f"Add {plan.SMOKE_NOTE}", "--repo", str(repo),
                "--scripted", str(self.scripted(responses))]
        return self.run(argv, cwd=self.sandbox.workspace, repo=repo, run_id=run_id)

    @property
    def lane_env_file(self) -> Path:
        """Where the `lane-env` check writes the trace dir and PATH the harness was given (`plan.smoke_checks`)."""
        return self.sandbox.home / "lane-env.txt"

    def run(self, argv: list[str], cwd: Path, repo: Path, run_id: str, timeout: float = 300) -> subprocess.CompletedProcess[str]:
        """Run a harness argv by hand from `cwd`, in the environment a lane gives it (`plan.lane_env`): the one
        `cox route launch` would build, which the launch tests hold cox to."""
        trace = self.sandbox.workspace / "runs" / f"{run_id}-trace"
        return _run(argv, env=plan.lane_env(self.sandbox.env, str(repo), str(trace)), cwd=cwd, timeout=timeout)


@pytest.fixture
def launchable(sandbox: Sandbox, repo: Repo, tmp_path: Path) -> Launchable:
    # The launcher a checkout carries at src/graphs/shell.py; the package does not ship it, so the install has none.
    shutil.copy2(ROOT / "src" / "graphs" / "shell.py", sandbox.install.root / "shell.py")
    cartridges, worktrees = tmp_path / "cartridges", tmp_path / "worktrees"
    shutil.copytree(ROOT / "src" / "cartridges" / "cartridges" / "base", cartridges / "base")
    (cartridges / TEAM).mkdir()
    (cartridges / TEAM / "cartridge.yaml").write_text(plan.team_cartridge_text(TEAM, str(worktrees), plan.smoke_checks(str(sandbox.home / "lane-env.txt"))), encoding="utf-8")
    # The target repository's own venv, holding the probe `lane-probe` looks for; git ignores it, as a real one is.
    probe = repo.path / ".venv" / "bin" / plan.LANE_PROBE
    probe.parent.mkdir(parents=True)
    probe.write_text(plan.LANE_PROBE_TEXT, encoding="utf-8")
    probe.chmod(0o755)
    with open(repo.path / ".git" / "info" / "exclude", "a", encoding="utf-8") as exclude:
        exclude.write(".venv/\n")
    skills = tmp_path / "skills"
    for relative, text in plan.skill_plugin_files().items():
        (skills / relative).parent.mkdir(parents=True, exist_ok=True)
        (skills / relative).write_text(text, encoding="utf-8")
    config = sandbox.home / ".config" / "agent-tools"
    provider = config / "provider.yaml"
    provider.write_text(plan.scripted_provider_profile_text(sandbox.store_url, str(sandbox.home / "scripted.json")),
                        encoding="utf-8")
    (config / "profile.yaml").write_text(
        plan.routing_profile_text(team=TEAM, workspace=str(sandbox.workspace), harness_dir=str(sandbox.install.root),
                                  provider_profile=str(provider), extra={"cartridges_dir": str(cartridges), "skills_roots": f"[{skills}]", "assume": "a"}),
        encoding="utf-8",
    )
    return Launchable(sandbox=sandbox, cartridges=cartridges, skills=skills, worktrees=worktrees)


# --- the sandbox forge ---------------------------------------------------------------------------------------------

API_TIMEOUT_S = 15


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", f"{plan.FORGE_MARK}: needs the sandbox forge, a Forgejo container (docs/smoke.md)")


@pytest.hookimpl(trylast=True)  # after `-m` has deselected, so a shard's weight counts the tests it will run
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """With COX_SMOKE_SHARD=<k>/<n>, keep only the files `plan.shard_files` puts in shard k (CI's parallel smoke jobs)."""
    value = os.environ.get(plan.SHARD_ENV)
    if not value or not plan.shard_applies(list(config.args)):
        return
    try:
        k, n = plan.parse_shard(value)
    except ValueError as error:
        raise pytest.UsageError(str(error)) from None
    files = [item.nodeid.split("::")[0] for item in items]
    keep = set(plan.shard_files({name: files.count(name) for name in files}, n)[k - 1])
    dropped = [item for item, name in zip(items, files, strict=True) if name not in keep]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = [item for item, name in zip(items, files, strict=True) if name in keep]


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
    """Make `image` present locally. A digest-pinned image already in the local store is used as is (`plan.pull_needed`):
    `docker pull` consults the credential helper even then, which fails in a non-interactive macOS session. Otherwise
    `docker pull`, tried `plan.PULL_ATTEMPTS` times with a short pause, so one registry error does not fail the run."""
    present = plan.is_digest_pinned(image) and _run(plan.docker_inspect_argv(image)).returncode == 0
    if not plan.pull_needed(image, present):
        return
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


# --- GitHub, replayed for the real gh ------------------------------------------------------------------------------

GH_RECORDINGS = ROOT / "smoke" / "gh_recordings"


@dataclass(frozen=True)
class GitHub:
    """The sandbox with the real `gh` on PATH and pointed at the replay (or, recording, at api.github.com), and a
    clone whose origin is `plan.GH_SLUG` on `plan.GH_HOST`, where `gh` finds the repository as it would in a checkout."""

    sandbox: Sandbox
    clone: Path


def _gh_version(gh: str) -> str:
    done = _run([gh, "--version"], timeout=30)
    return done.stdout.splitlines()[0] if done.returncode == 0 and done.stdout else "unknown"


@pytest.fixture
def github(sandbox: Sandbox, tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[GitHub]:
    gh = gh_replay.real_gh()
    if gh is None:
        _unavailable(plan.unavailable("no gh on PATH", required=_required()))
    recording = GH_RECORDINGS / plan.gh_recording_name(request.node.name)
    recording_mode = plan.is_required(os.environ.get(plan.GH_RECORD_ENV))
    if recording_mode:
        token = os.environ.get(plan.GH_TOKEN_ENV)
        if not token:
            pytest.fail(f"{plan.GH_RECORD_ENV}=1 records from api.github.com and needs a token in {plan.GH_TOKEN_ENV}",
                        pytrace=False)
        server: gh_replay.Replay | gh_replay.Recorder = gh_replay.Recorder(token)
    elif not recording.is_file():
        pytest.fail(f"no recording at {recording}; run this test with {plan.GH_RECORD_ENV}=1 to make one", pytrace=False)
    else:
        server = gh_replay.Replay(gh_replay.load(recording)["exchanges"])
    clone = tmp_path / "gh-clone"
    _must(["git", "init", "--quiet", "-b", "main", str(clone)], env=sandbox.env)
    _must(["git", "-C", str(clone), "remote", "add", "origin", plan.gh_remote_url(plan.GH_SLUG)], env=sandbox.env)
    env = {**sandbox.env, **plan.gh_env(server.proxy, str(tmp_path / "gh-config")),
           "PATH": os.pathsep.join([sandbox.env["PATH"], str(Path(gh).parent)])}
    with gh_replay.serving(server):
        yield GitHub(sandbox=replace(sandbox, env=env), clone=clone)
    if isinstance(server, gh_replay.Recorder):
        gh_replay.save(recording, {"gh": _gh_version(gh), "slug": plan.GH_SLUG, "exchanges": server.exchanges})
    if server.misses:
        pytest.fail(f"gh asked what {recording.name} has no answer for: {server.misses}", pytrace=False)


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


def _newest_migration() -> int:
    """The highest `store_ddl_NNNN` number this tree carries, read from its tracked files."""
    listing = _must(["git", "-C", str(ROOT), "ls-files", "src/graphs/harness/"]).stdout
    return max(int(m.group(1)) for line in listing.splitlines() if (m := re.search(r"store_ddl_(\d{4})\.py$", line)))


def _additive_migration_text(version: int) -> str:
    """One more migration, additive: it declares `ADDITIVE = True` and only adds a table."""
    return (
        f'"""Migration {version:04d}: a smoke-only table, one past this tree\'s newest."""\n\n'
        f"VERSION = {version}\n"
        'DESCRIPTION = "smoke edge table"\n'
        "ADDITIVE = True\n\n\n"
        "def statements(dialect):\n"
        '    return ("CREATE TABLE IF NOT EXISTS smoke_edge (key TEXT NOT NULL, PRIMARY KEY (key))",)\n'
    )


def _edge_origin(forge: Forge, tmp_path_factory: pytest.TempPathFactory, label: str,
                 newer_files: dict[str, str]) -> EdgeOrigin:
    """This tree as one commit on the forge, and one commit more that writes `newer_files` (path: text). CI is
    reported green on both, through commit statuses, as Forgejo Actions would."""
    work = tmp_path_factory.mktemp(label)
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
    for relative, text in newer_files.items():
        (tree / relative).write_text(text, encoding="utf-8")
        _git(env, tree, "add", relative)
    _git(env, tree, "commit", "--quiet", "-m", "smoke: one commit newer")
    newer = _git(env, tree, "rev-parse", "HEAD")
    name = plan.forge_repo_name(f"{label}-{SESSION}")
    _publish(forge, env, name, tree)
    for sha in (base, newer):
        forge.must("POST", f"/repos/{forge.user}/{name}/statuses/{sha}", plan.commit_status_body("success", "ci"))
    return EdgeOrigin(name=name, base=base, newer=newer)


@pytest.fixture(scope="module")
def edge_origin(forge: Forge, tmp_path_factory: pytest.TempPathFactory) -> EdgeOrigin:
    """A coxswain repository on the forge: this tree, then one commit more that changes one file."""
    return _edge_origin(forge, tmp_path_factory, "edge", {"smoke-edge.txt": "one commit newer\n"})


@pytest.fixture(scope="module")
def edge_origin_additive(forge: Forge, tmp_path_factory: pytest.TempPathFactory) -> EdgeOrigin:
    """A coxswain repository on the forge: this tree, then one commit more that adds an additive store migration, one
    past this tree's newest."""
    version = _newest_migration() + 1
    path = f"src/graphs/harness/store_ddl_{version:04d}.py"
    return _edge_origin(forge, tmp_path_factory, "edge-additive", {path: _additive_migration_text(version)})


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
    return _edge_machine(forge_sandbox, forge, edge_origin, uv_shared)


@pytest.fixture
def edge_machine_additive(forge_sandbox: Sandbox, forge: Forge, edge_origin_additive: EdgeOrigin,
                          uv_shared: dict[str, str]) -> EdgeMachine:
    """An edge machine whose newer green commit adds an additive store migration."""
    return _edge_machine(forge_sandbox, forge, edge_origin_additive, uv_shared)


def _edge_machine(forge_sandbox: Sandbox, forge: Forge, edge_origin: EdgeOrigin, uv_shared: dict[str, str]) -> EdgeMachine:
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


# --- the sandbox object store --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ObjectStore:
    """The session's Garage: its S3 endpoint, its one bucket, and the key the suite imported into it."""

    endpoint: str
    bucket: str
    access_key: str
    secret_key: str


def _until_ok(argv: list[str], what: str) -> subprocess.CompletedProcess[str]:
    """Run `argv` until it exits 0, for at most `READY_TIMEOUT_S`; a server still starting refuses the first tries."""
    deadline = time.monotonic() + READY_TIMEOUT_S
    while (done := _run(argv)).returncode != 0:
        if time.monotonic() > deadline:
            raise RuntimeError(f"{what} did not succeed after {READY_TIMEOUT_S}s: {done.stderr.strip()[-300:]}")
        time.sleep(0.25)
    return done


@pytest.fixture(scope="session")
def object_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ObjectStore]:
    """A single-node Garage container on a loopback port, with one bucket and one key, removed at the end of the
    session. The key is chosen here and imported, so no command output the fixture reads carries the secret."""
    docker = shutil.which("docker")
    broken_ok = plan.is_required(os.environ.get(plan.BROKEN_DOCKER_OK_ENV))
    decision = plan.container_source("the sandbox object store", docker, _docker_ok(docker), required=_required(),
                                     broken_docker_ok=broken_ok)
    if isinstance(decision, plan.Skip | plan.Fail):
        _unavailable(decision)
    _reap_stale()
    _pull(plan.GARAGE_IMAGE)
    config = tmp_path_factory.mktemp("garage") / "garage.toml"
    config.write_text(plan.garage_config_text(secrets.token_hex(32)), encoding="utf-8")
    config.chmod(0o644)  # the container's user reads it through the read-only mount
    container = _must(plan.garage_run_argv(SESSION, int(time.time()), str(config))).stdout.strip()
    try:  # from the moment the container exists, every way out of this fixture removes it
        node = plan.parse_node_id(_until_ok(plan.garage_argv(container, "node", "id"), "garage node id").stdout)
        key_id, secret = f"GK{secrets.token_hex(12)}", secrets.token_hex(32)
        for argv in plan.garage_setup_argvs(container, node, key_id, secret):
            _until_ok(argv, f"garage {' '.join(argv[4:6])}")
        port = plan.parse_published_port(_must(plan.docker_port_argv_for(container, plan.GARAGE_S3_PORT)).stdout)
        yield ObjectStore(endpoint=plan.garage_endpoint(port), bucket=plan.GARAGE_BUCKET, access_key=key_id, secret_key=secret)
    finally:
        _run(plan.docker_remove_argv(container))
