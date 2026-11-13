import os
import re
import sys

import pytest

from harness.serial_tests import pytest_collection_modifyitems, pytest_sessionfinish  # noqa: F401  (hooks)

# sqlite is the suites' stand-in for the one real store, Postgres. Outside the suites it does not open.
os.environ.setdefault("COXSWAIN_STORE_DOUBLE", "sqlite")

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
def _fresh_store_url():
    """run_store caches the store URL per process; each test starts without it."""
    from agent_tools import console_screen, run_store

    caches = (run_store._store_url_for, run_store._lease_table, run_store._lease_rows, run_store._traces_root_for,
              run_store._harness_python_for, run_store._harness_dump, run_store._found_parquet_rows,
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


@pytest.fixture(autouse=True)
def _no_host_profile(monkeypatch, tmp_path_factory):
    """The default profile is not absent in tests: `~/.config/agent-tools/profile.yaml` is read unless a test names another."""
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(tmp_path_factory.getbasetemp() / "no-profile.yaml"))


@pytest.fixture(autouse=True)
def _no_real_harness(monkeypatch):
    """No test reaches the machine's real harness: a store_cli call would write leases into a real store. A test that
    needs a harness monkeypatches `store_cli._harness_python` itself."""
    from agent_tools import store_cli

    monkeypatch.setattr(store_cli, "_harness_python", lambda: None)


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
    this interpreter's environment, against the sqlite file in the test's own runs directory. sqlite is the test
    double for the Postgres store; nothing here reaches a real one."""
    import sys
    from pathlib import Path

    from agent_tools import run_store

    migrated: set[str] = set()
    real_run, real_readonly = run_store._queue_run, run_store.connect_readonly

    def ensure(runs_dir) -> None:
        from harness.store_dialect import connect
        from harness.store_migrate import default_modules, migrate

        url = run_store._store_url(Path(runs_dir))
        if not url.startswith("sqlite") or url in migrated:
            return
        Path(runs_dir).mkdir(parents=True, exist_ok=True)
        conn = connect(url)
        try:
            migrate(conn, "2026-01-01T00:00:00Z", default_modules())
        finally:
            conn.close()
        migrated.add(url)

    def run(runs_dir, build):
        ensure(runs_dir)
        return real_run(runs_dir, build)

    def readonly(runs_dir):
        ensure(runs_dir)
        return real_readonly(runs_dir)

    monkeypatch.setattr(run_store, "_harness_python", lambda: Path(sys.executable))
    monkeypatch.setattr(run_store, "_queue_run", run)
    monkeypatch.setattr(run_store, "connect_readonly", readonly)


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
