import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from harness import extras_tests
from harness.serial_tests import pytest_collection_modifyitems, pytest_sessionfinish  # noqa: F401  (hooks)

# sqlite is the suites' stand-in for the one real store, Postgres. Outside the suites it does not open.
os.environ.setdefault("COXSWAIN_STORE_DOUBLE", "sqlite")
# `cox setup doctor --fix` starts the warm smoke Postgres where docker answers; the unit suites never touch docker.
# smoke/test_doctor_warm_postgres.py runs the real thing in its own sandbox.
os.environ.setdefault("COX_DOCTOR_SKIP_SMOKE_POSTGRES", "1")


def pytest_configure(config) -> None:
    extras_tests.register(config)  # the extras jobs' marks and the unit jobs' skip guard

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(autouse=True)
def _no_cached_core_or_harness_module():
    """`_schema_versions()` does a real `import core` / `import harness`; a
    fake package one test puts on `sys.path` must not leak, via the module
    cache, into a test that runs after it expects the real absence."""
    for name in ("core", "harness"):
        sys.modules.pop(name, None)
    yield
    for name in ("core", "harness"):
        sys.modules.pop(name, None)


def strip_ansi(text: str) -> str:
    """Removes SGR escape sequences, so a help-text assertion reads the same
    whether or not argparse's colorizer is active (Python 3.14 colours help
    output whenever FORCE_COLOR is set, as the cox-launched session does)."""
    return _ANSI.sub("", text)


@pytest.fixture(autouse=True)
def _store_backups_stay_out_of_home(tmp_path_factory, monkeypatch):
    """`harness.store_migrate.open_store` backs a store with a migration pending up first (`should_back_up`; a fresh
    one, holding no table, is not) to `$COX_STORE_BACKUP_DIR`, else to `~/.local/state/coxswain/backups` under the
    real home. In this suite that was once about 290 tests a run, each leaving an empty `store-v0-to-v<N>` file in
    the operator's home. Point it at a tmp directory of its own, as the graphs suite does; the variable reaches every
    `cox` and harness subprocess a test starts too. A directory from `tmp_path_factory`, not `tmp_path`, so tests that
    inventory their own `tmp_path` see no extra."""
    monkeypatch.setenv("COX_STORE_BACKUP_DIR", str(tmp_path_factory.mktemp("store-backups")))


@pytest.fixture(autouse=True)
def _sqlite_double_skips_disk_syncs(monkeypatch):
    """A test's sqlite store is thrown away, so its commits need no disk sync (`store_dialect.sqlite_sync_off`)."""
    monkeypatch.setenv("COX_SQLITE_SYNC_OFF", "1")


@pytest.fixture(autouse=True)
def _fresh_store_url():
    """run_store caches the store URL per process; each test starts without it."""
    from agent_tools import console_screen, run_store

    caches = (run_store._store_url_for, run_store.lease_table_for, run_store._lease_rows, run_store._traces_root_for,
              run_store._found_parquet_rows,
              run_store._store_ids_by_trace, console_screen._work_items_snapshot)
    for cache in caches:
        cache.cache_clear()
    yield
    for cache in caches:
        cache.cache_clear()


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


_SOURCE_ROOTS = tuple(str(Path(__file__).resolve().parents[2] / name) for name in ("tools", "graphs", "cartridges"))


@pytest.fixture(scope="session", autouse=True)
def _child_interpreters_import_this_tree():
    """A child `sys.executable` (the store double, the land's leases) imports the source roots pytest.ini puts on
    `sys.path`, not the checkout an editable install in the venv points at: that one may be older than this tree."""
    inherited = os.environ.get("PYTHONPATH", "")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PYTHONPATH", os.pathsep.join(p for p in (*_SOURCE_ROOTS, inherited) if p))
        yield


@pytest.fixture(autouse=True)
def _no_host_profile(monkeypatch, tmp_path_factory):
    """The default profile is not absent in tests: `~/.config/agent-tools/profile.yaml` is read unless a test names another."""
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(tmp_path_factory.getbasetemp() / "no-profile.yaml"))


@pytest.fixture(autouse=True)
def _no_real_usage_meter(monkeypatch, request):
    """No test reads the box's real status-line meter or implied-ceiling file: this machine carries a
    live, fresh `~/.local/state/coxswain/rate-limits.json`, so without this a test that never mentions
    `usage_meter` would silently build its window from whatever the real meter says right now. A test
    that wants meter behaviour monkeypatches `usage_meter.read` / `implied_ceiling` itself.

    `test_usage_meter` exercises these functions' own file-reading logic against paths it names
    explicitly, so it is exempt: patching them there would test the patch, not the module."""
    if request.module.__name__ == "test_usage_meter":
        return

    from agent_tools import usage_meter

    monkeypatch.setattr(usage_meter, "read", lambda *a, **k: None)
    monkeypatch.setattr(usage_meter, "implied_ceiling", lambda *a, **k: None)
    monkeypatch.setattr(usage_meter, "reconcile_implied_ceilings", lambda *a, **k: frozenset())


@pytest.fixture
def board_store(monkeypatch):
    """The store as the board, for a test that drives a command end to end: queue reads and writes run for real, in
    process, against the sqlite file in the test's own runs directory, migrated before a direct read opens it. sqlite
    is the test double for the Postgres store; nothing here reaches a real one."""
    from pathlib import Path

    from agent_tools import run_store

    migrated: set[str] = set()
    real_readonly = run_store.connect_readonly

    def ensure(runs_dir) -> None:
        from harness.store_dialect import connect
        from harness.store_migrate import default_modules, migrate

        url = run_store.store_url_for(Path(runs_dir))
        if not url.startswith("sqlite") or url in migrated:
            return
        Path(runs_dir).mkdir(parents=True, exist_ok=True)
        conn = connect(url)
        try:
            migrate(conn, "2026-01-01T00:00:00Z", default_modules())
        finally:
            conn.close()
        migrated.add(url)

    def readonly(runs_dir):
        ensure(runs_dir)
        return real_readonly(runs_dir)

    monkeypatch.setattr(run_store, "connect_readonly", readonly)


@pytest.fixture
def store_at(monkeypatch, tmp_path_factory):
    """A function that points every store call at `url` for the rest of the test, through a routing profile whose
    provider profile carries `storage_url`. `nosuch://x` or an unreachable `postgresql://` makes the store unopenable."""

    def point(url: str) -> None:
        base = tmp_path_factory.mktemp("store-at")
        provider = base / "provider.yaml"
        provider.write_text(f"storage_url: {url}\n", encoding="utf-8")
        routing = base / "routing.yaml"
        routing.write_text(f"provider_profile: {provider}\n", encoding="utf-8")
        monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(routing))

    return point


@pytest.fixture(autouse=True)
def _no_real_conversation_file(monkeypatch, tmp_path_factory):
    """A bare launch that attaches over MCP stores a conversation id in the state directory. No test writes the
    machine's real one: the launcher's own lookup is pointed at a directory of the test session's."""
    from agent_tools import cli

    home = tmp_path_factory.getbasetemp() / "no-state-home"
    real = cli.chair_attach.conversation_path
    monkeypatch.setattr(cli, "_conversation_file", lambda: real(os.environ.get("XDG_STATE_HOME") or str(home), home))


@pytest.fixture(autouse=True)
def _no_token_in_the_environment(monkeypatch):
    """The token status and the doctor count a token kept in the environment. A machine that keeps its real one
    there must not change what a test sees: a test that wants one sets it."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)


SHORT_TEMP_ROOT = Path("/tmp")


@pytest.fixture
def short_tmp_path():
    """A fresh directory whose path is short whatever the temp root is. Claude's project folder is the whole workspace
    path in one file name, so a test that makes one for a workspace under `tmp_path` failed with "File name too long"
    when the run's temp root was long, as a lane's TMPDIR is. A module that names project folders overrides `tmp_path`
    with this one."""
    root = SHORT_TEMP_ROOT if SHORT_TEMP_ROOT.is_dir() else Path(tempfile.gettempdir())
    path = Path(tempfile.mkdtemp(prefix="t-", dir=root)).resolve()
    yield path
    shutil.rmtree(path, ignore_errors=True)
