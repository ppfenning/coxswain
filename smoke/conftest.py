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
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from smoke import plan

ROOT = Path(__file__).resolve().parents[1]
SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"
READY_TIMEOUT_S = 120


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


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

    @property
    def cox(self) -> Path:
        return self.bin / "cox"


def _hashes(base: Path, folders: list[str]) -> dict[str, str]:
    found = [f"{folder}/{p.name}" for folder in folders for p in (base / folder).glob("*.py")]
    return {rel: hashlib.sha256((base / rel).read_bytes()).hexdigest() for rel in plan.python_sources(found)}


def _install_drift(site: Path) -> list[str]:
    tool = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]
    dirs = plan.package_dirs(tool["packages"], tool["package-dir"])
    # keyed by the installed path, so the two sides compare file for file
    tree = {inst + rel[len(src):]: digest for src, inst in dirs.values() for rel, digest in _hashes(ROOT, [src]).items()}
    installed = _hashes(site, [inst for _, inst in dirs.values()])
    return plan.install_drift(tree, installed)


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
    site = Path(_must([str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"]).stdout.strip())
    drift = _install_drift(site)
    if drift:
        pytest.fail(f"the install is not this tree: {drift[:5]}", pytrace=False)
    return Install(root=root, python=python, bin=venv / "bin", site=site)


# --- Postgres ------------------------------------------------------------------------------------------------------


def _docker_ok(docker: str | None) -> bool:
    return docker is not None and _run([docker, "info", "--format", "{{.ServerVersion}}"], timeout=30).returncode == 0


def _start_container() -> tuple[str, int]:
    container = _must(plan.docker_run_argv()).stdout.strip()
    deadline = time.monotonic() + READY_TIMEOUT_S
    while _run(plan.docker_ready_argv(container)).returncode != 0:
        if time.monotonic() > deadline:
            _run(plan.docker_remove_argv(container))
            raise RuntimeError(f"Postgres in {container[:12]} was not ready after {READY_TIMEOUT_S}s")
        time.sleep(0.25)
    return container, plan.parse_published_port(_must(plan.docker_port_argv(container)).stdout)


@pytest.fixture(scope="session")
def postgres() -> Iterator[str]:
    """The URL of the session's Postgres server: the one `COX_SMOKE_POSTGRES_URL` names, else a container started here
    and removed at the end of the session."""
    docker = shutil.which("docker")
    url = os.environ.get(plan.URL_ENV)
    decision = plan.postgres_source(url, docker, url is None and _docker_ok(docker), required=_required())
    if isinstance(decision, plan.Skip | plan.Fail):
        _unavailable(decision)
    if isinstance(decision, plan.UseUrl):
        yield decision.url
        return
    container, port = _start_container()
    try:
        yield plan.postgres_url(port)
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
    _must([str(install.python), "-c", _ADMIN, postgres, "create", name])
    url = plan.with_database(postgres, name)
    _must([str(install.python), "-m", "harness.store_cli", "--store-url", url, "host", "list"], cwd=install.root)
    try:
        yield url
    finally:
        _run([str(install.python), "-c", _ADMIN, postgres, "drop", name])


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
    return Repo(path=path, origin=origin)
