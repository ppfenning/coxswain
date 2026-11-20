import os

import pytest

from devtools import cli
from harness import extras_tests

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
