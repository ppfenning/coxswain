import pytest

# The fixtures and the `_land` driver are the store-mode land harness; this file only asserts one output line.
from test_land_from_store import _land, _own_tempdir, repo, store  # noqa: F401

from agent_tools import cli

_REMOVED_LINE = "its items are already done, so there is no approved check"


@pytest.mark.parametrize("state", ["approved", "done"])
def test_a_store_mode_phase_land_never_claims_its_items_are_already_done(repo, tmp_path, monkeypatch, store, capsys, state):  # noqa: F811
    store.states = [state]
    rc, ran = _land(repo, tmp_path, monkeypatch, store, extra=("--phase", "seams"), phase=True)
    out = capsys.readouterr().out
    assert (rc, ran[-1]) == (0, "mark_done")
    assert "forge:" in out
    assert _REMOVED_LINE not in out
    assert "no approved check" not in out


def test_a_phase_land_whose_pr_is_already_merged_prints_one_marker_line(repo, tmp_path, monkeypatch, store, capsys):  # noqa: F811
    monkeypatch.setattr(cli, "_phase_merged_pr", lambda *args: {"number": 535, "state": "merged"})
    rc, ran = _land(repo, tmp_path, monkeypatch, store, extra=("--phase", "seams"), phase=True)
    lines = capsys.readouterr().out.splitlines()
    assert (rc, ran) == (0, ["clean_phase", "mark_done"])
    assert lines.count("merge: already merged (PR #535)") == 1


def test_a_phase_land_with_no_merged_pr_never_prints_the_marker(repo, tmp_path, monkeypatch, store, capsys):  # noqa: F811
    monkeypatch.setattr(cli, "_phase_merged_pr", lambda *args: None)
    _land(repo, tmp_path, monkeypatch, store, extra=("--phase", "seams"), phase=True)
    assert "merge: already merged" not in capsys.readouterr().out
