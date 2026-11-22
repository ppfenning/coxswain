import os
from pathlib import Path

import pytest

from devtools import tree_imports

# A lane's task worktree links the shared venv, whose editable install maps the packages to the main checkout: this
# tree's src/ goes first, or the suite refuses to load, so a change that breaks them cannot pass here unseen.
tree_imports.use_this_tree(Path(__file__).resolve().parents[1])

from devtools import cli  # noqa: E402
from harness import extras_tests  # noqa: E402

# sqlite is the suites' stand-in for the one real store, Postgres. Outside the suites it does not open.
os.environ.setdefault("COXSWAIN_STORE_DOUBLE", "sqlite")


def pytest_configure(config) -> None:
    extras_tests.register(config)  # the extras jobs' marks and the unit jobs' skip guard


@pytest.fixture(autouse=True)
def _store_backups_stay_out_of_home(tmp_path_factory, monkeypatch):
    """A store a test opens with a migration pending is backed up first (`harness.store_migrate.should_back_up`),
    by default under the real home; here it goes to a tmp directory of its own, as in the graphs and tools suites."""
    monkeypatch.setenv("COX_STORE_BACKUP_DIR", str(tmp_path_factory.mktemp("store-backups")))


@pytest.fixture(autouse=True)
def _sqlite_double_skips_disk_syncs(monkeypatch):
    """A test's sqlite store is thrown away, so its commits need no disk sync (`store_dialect.sqlite_sync_off`)."""
    monkeypatch.setenv("COX_SQLITE_SYNC_OFF", "1")


@pytest.fixture(autouse=True)
def _clean_trees(monkeypatch):
    """Release tests run against fake checkouts whose fake git records every call,
    so the clean gate stands aside there; its own tests
    (test_release_clean_gate.py) override this fixture and run it for real."""
    monkeypatch.setattr(cli, "_clean_gate", lambda steps, root, overrides, umbrella, environ: ([], None))


@pytest.fixture(autouse=True)
def _no_rehearsal_lookup(monkeypatch):
    """The rehearsal gate reads gh and git, which the fake runners here do not answer; its own
    tests (test_release.py, test_release_rehearsal_gate.py) put the real edge back or call `decide`."""
    monkeypatch.setattr(cli, "_rehearsal_steps", lambda *args: [])
