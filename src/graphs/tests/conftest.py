"""Synthetic fixtures only. Obvious fakes, never a sampled workspace."""

from __future__ import annotations

import os
import uuid
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


@pytest.fixture
def at_task(cart, monkeypatch):
    """The test's cartridge lands per task. Phases stack on each other and on the default ref only there: at
    `initiative` every phase is cut from the initiative branch. A fake forge stands in, so no run reaches `gh`."""
    from harness import epic

    cart["landing_areas"]["land_at"] = "task"
    monkeypatch.setattr(epic, "_push_review_ref", lambda ctx, local, name: (True, ""))
    monkeypatch.setattr(epic, "_existing_pr", lambda ctx, head: (True, ""))
    monkeypatch.setattr(epic, "_open_review_pr", lambda ctx, *, title, body, head, base: (True, f"pr:{head}"))
    return cart
