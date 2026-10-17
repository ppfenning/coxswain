import pytest

from devtools import cli


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
