import pytest

from devtools import cli


@pytest.fixture(autouse=True)
def _clean_trees(monkeypatch):
    """Release tests run against fake checkouts whose fake git records every call,
    so the clean gate stands aside there; its own tests
    (test_release_clean_gate.py) override this fixture and run it for real."""
    monkeypatch.setattr(cli, "_clean_gate", lambda steps, root, overrides, umbrella, environ: ([], None))
