"""Synthetic fixtures only. Obvious fakes, never a sampled workspace."""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Mapping
from typing import NamedTuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest

from harness import termination
from harness.serial_tests import pytest_collection_modifyitems, pytest_sessionfinish  # noqa: F401  (hooks)

CARTRIDGE = {
    "team": "acme",
    "cartridge_sha": "sha-fixture",
    "cartridge_dir": "/fake/cartridges/acme",
    "context": [],
    "skills": {"plan": "acme-skills:plan", "build": "acme-skills:build"},
    "write_kinds": {
        "draft_pr_create": {"risk": "low", "ramp": "eligible"},
        "comment_add": {"risk": "low", "ramp": "gated"},
        "merge": {"risk": "high", "ramp": "never"},
    },
    "landing_areas": {"worktree_root": "~/worktrees"},
}


def pytest_configure(config) -> None:
    config.addinivalue_line("markers", "real_courier: leave harness.courier_adapter.send unstubbed for this test")


@pytest.fixture(autouse=True)
def _courier_sends_are_recorded(request, monkeypatch):
    """Stub the courier send so no test files a real note or waits out its 10 s timeout.

    epic.py binds the function with a from-import (`send_to_courier`), so patching
    `courier_adapter.send` alone leaves epic holding the real one; both names are patched.
    The recorded `(ref, to, note)` tuples are the fixture value and also `courier_adapter.send.calls`.
    A test marked `real_courier` gets the real send.
    """
    if request.node.get_closest_marker("real_courier") is not None:
        return None
    from harness import courier_adapter, epic

    calls: list[tuple[str, str, str]] = []

    def record(ref: str, to: str, note: str) -> bool:
        calls.append((ref, to, note))
        return True

    record.calls = calls  # type: ignore[attr-defined]
    monkeypatch.setattr(courier_adapter, "send", record)
    monkeypatch.setattr(epic, "send_to_courier", record)

    exits: list[tuple[str, str, str]] = []

    def record_exit(run_id: str, initiative: str, outcome: str) -> bool:
        exits.append((run_id, initiative, outcome))
        return True

    record_exit.calls = exits  # type: ignore[attr-defined]
    monkeypatch.setattr(courier_adapter, "publish_run_exited", record_exit)
    monkeypatch.setattr(epic, "publish_run_exited", record_exit)
    return calls


@pytest.fixture
def cartridge() -> dict:
    return {k: (v.copy() if isinstance(v, (dict, list)) else v) for k, v in CARTRIDGE.items()}


@pytest.fixture
def plan_response() -> dict:
    return {"steps": ["read the failing test", "fix it"], "files_expected": ["src/a.py"], "out_of_scope": ["the CLI"]}


@pytest.fixture
def build_response() -> dict:
    return {
        "patch": "--- a/src/a.py\n+++ b/src/a.py\n-old line\n+new line\n+another\n",
        "summary": "fix the off-by-one",
        "files_touched": ["src/a.py"],
        "commands_run": [{"command": "pytest -q", "output": "1 passed"}],
    }


@pytest.fixture
def review_response() -> dict:
    return {"verdict": "approve", "findings": [], "rationale": "matches the charter"}


T0 = "2026-09-24T00:00:00Z"


def with_search_path(url: str, schema: str) -> str:
    """`url` with libpq's `options=-csearch_path=<schema>` added to its query string."""
    parts = urlsplit(url)
    query = [*parse_qsl(parts.query), ("options", f"-csearch_path={schema}")]
    return urlunsplit(parts._replace(query=urlencode(query)))


@pytest.fixture(params=["sqlite", "postgres"])
def store_url(request):
    """A store URL per backend. Postgres runs only when COXSWAIN_TEST_PG_URL is set, in a schema of its own."""
    if request.param == "sqlite":
        yield "sqlite:///:memory:"
        return
    base = os.environ.get("COXSWAIN_TEST_PG_URL")
    if not base:
        pytest.skip("COXSWAIN_TEST_PG_URL is not set")
    import psycopg

    schema = f"t_{uuid.uuid4().hex}"
    admin = psycopg.connect(base, autocommit=True)
    try:
        admin.execute(f"CREATE SCHEMA {schema}")
        yield with_search_path(base, schema)
    finally:
        try:
            admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            admin.close()


@pytest.fixture
def store_conn(store_url):
    from harness.store_migrate import open_store

    c = open_store(store_url, T0)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture(autouse=True)
def _store_backups_stay_out_of_home(tmp_path_factory, monkeypatch):
    """Every test that opens a fresh store through `store_conn`/`open_store` now backs it up first.

    Route that backup at a tmp directory of its own instead of the real $HOME the code defaults
    to, so the other ~30 test files that never meant to test backup behaviour do not write there.
    A directory from `tmp_path_factory`, not the test's own `tmp_path`: several tests inventory
    every file under their own `tmp_path` and a stray backup directory there would be extra.
    Tests in test_store_migrate.py that examine `COX_STORE_BACKUP_DIR` set their own value, which
    simply overrides this default.
    """
    monkeypatch.setenv("COX_STORE_BACKUP_DIR", str(tmp_path_factory.mktemp("store-backups")))


@pytest.fixture(autouse=True)
def _termination_flag_is_per_test():
    """`termination` is process state that nothing clears; a test that sets it must not leave the next test a killed run."""
    termination._TERMINATING.clear()
    yield
    termination._TERMINATING.clear()


_GIT_REPOSITORY_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
)


@pytest.fixture(scope="session", autouse=True)
def _git_templates_are_empty(tmp_path_factory):
    """`git init` and `git clone` copy the stock templates (sample hooks, description, info/exclude) unless the template holds none.

    The template keeps empty `hooks/` and `info/` directories: tests write hook scripts and `info/exclude` into them."""
    templates = tmp_path_factory.mktemp("git-templates")
    for name in ("hooks", "info"):
        (templates / name).mkdir()
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("GIT_TEMPLATE_DIR", str(templates))
        yield


@pytest.fixture(autouse=True)
def _git_environment_is_hermetic(tmp_path_factory, monkeypatch) -> None:
    """Git reads only the repository it is pointed at, so no inherited variable or global config may reach a child git.

    The config file comes from `tmp_path_factory`, not `tmp_path`: several tests inventory every file under their own.
    """
    for name in _GIT_REPOSITORY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    empty_config = tmp_path_factory.mktemp("git-config") / "gitconfig"
    empty_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    # The harness commits without its own identity; an empty global config no longer supplies one.
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "t")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "t@invalid")


GUARDED_MODULES = (
    "test_epic_waves",
    "test_epic_driver",
    "test_lane_workers",
    "test_epic_lane_workers",
    "test_checks_workers",
    "test_epic_timeouts",
    "test_phase_runner",
    "test_epic_stale_phase_worktree",
    "test_epic_stale_base",
    "test_epic_isolation",
    "test_epic_phase_isolation",
)


class Snapshot(NamedTuple):
    threads: frozenset[tuple[int, str]]  # (ident, name) of each live non-daemon thread
    cwd: str
    env: Mapping[str, str]


def isolation_verdict(before: Snapshot, after: Snapshot) -> tuple[str, ...]:
    """One finding per leaked thread, changed cwd and differing env var; `()` is a clean run."""
    threads = tuple(f"thread {name!r} is still alive" for _, name in sorted(after.threads - before.threads))
    cwd = (f"cwd changed from {before.cwd!r} to {after.cwd!r}",) if before.cwd != after.cwd else ()
    env = tuple(
        f"env {key} changed from {before.env.get(key)!r} to {after.env.get(key)!r}"
        for key in sorted(before.env.keys() | after.env.keys())
        if before.env.get(key) != after.env.get(key)
    )
    return (*threads, *cwd, *env)


def _snapshot() -> Snapshot:
    live = frozenset((t.ident or 0, t.name) for t in threading.enumerate() if t.is_alive() and not t.daemon)
    try:
        cwd = os.getcwd()
    except OSError:  # the directory a test changed into was removed
        cwd = "<deleted>"
    # pytest rewrites PYTEST_CURRENT_TEST between setup and teardown; it is not the test's doing.
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    return Snapshot(live, cwd, env)


@pytest.fixture(autouse=True)
def _a_guarded_modules_leave_no_trace(request):
    """A guarded module's test may not leave a non-daemon thread, a changed cwd or a different environment.

    The name sorts first so pytest sets this fixture up before `monkeypatch` and tears it down after it:
    a test's own `monkeypatch.setenv` is undone by the time the comparison runs.
    """
    if request.module.__name__.rsplit(".", 1)[-1] not in GUARDED_MODULES:
        yield
        return
    before = _snapshot()
    yield
    findings = isolation_verdict(before, _snapshot())
    if findings:
        pytest.fail("test left state behind:\n" + "\n".join(findings), pytrace=False)
