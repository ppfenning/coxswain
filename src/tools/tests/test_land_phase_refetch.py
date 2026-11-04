import json
import subprocess as sp

# The fixtures and the `_land` driver are the store-mode land harness; this file only varies the phase branch.
from test_land_from_store import _land, _own_tempdir, repo, store  # noqa: F401

from agent_tools import cli, remote_lane

BRANCH = "epic/x/seams"
STOP = "land: stopped at fetch_phase_branch: phase branch not fetched from box"


def _remote_run(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "epic-x-5.remote.json").write_text(json.dumps({"host": "box", "launched_at": "2026-11-03T00:00:00Z"}))
    (runs / "epic-x-5.fetched.json").write_text(json.dumps({"fetched_at": "2026-11-03T00:01:00Z", "repos": [], "tasks": []}))


def _fetching(monkeypatch, checkout, *, brings):
    """Replace the remote fetch; it records each call and creates the phase branch in `checkout` when `brings`."""
    calls = []

    def fetch(runs_dir, hosts, run_id, mode="files", by=""):
        calls.append((run_id, mode))
        if brings:
            sp.run(["git", "-C", str(checkout), "branch", BRANCH], check=True, capture_output=True)
        return "fetched", [], "box"

    monkeypatch.setattr(cli, "_fetch_one", fetch)
    return calls


def _planned(monkeypatch):
    seen = []
    real = cli.land.land_plan

    def plan(*args, **kwargs):
        seen.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(cli.land, "land_plan", plan)
    return seen


def _conflicts(monkeypatch):
    marked = []
    monkeypatch.setattr(cli, "_mark_land_conflict", lambda *args: marked.append(args))
    return marked


def test_a_missing_phase_branch_is_fetched_again_and_then_planned(repo, tmp_path, monkeypatch, store, capsys):  # noqa: F811
    _remote_run(tmp_path)
    store.states = ["approved"]
    calls = _fetching(monkeypatch, repo, brings=True)
    planned = _planned(monkeypatch)
    rc, _ran = _land(repo, tmp_path, monkeypatch, store, extra=("--phase", "seams"), phase=True)
    assert calls == [("epic-x-5", "files")]
    assert planned == [True]
    assert "fetch_phase_branch" not in capsys.readouterr().out
    assert rc == 0


def test_a_branch_missing_on_the_remote_too_stops_with_the_named_reason(repo, tmp_path, monkeypatch, store, capsys):  # noqa: F811
    _remote_run(tmp_path)
    store.states = ["approved"]
    calls = _fetching(monkeypatch, repo, brings=False)
    planned = _planned(monkeypatch)
    marked = _conflicts(monkeypatch)
    rc, ran = _land(repo, tmp_path, monkeypatch, store, extra=("--phase", "seams"), phase=True)
    assert capsys.readouterr().out.splitlines()[-1] == STOP
    assert (rc, ran, planned, marked) == (2, [], [], [])
    assert len(calls) == 1


def test_a_phase_branch_already_here_is_not_fetched(repo, tmp_path, monkeypatch, store):  # noqa: F811
    _remote_run(tmp_path)
    sp.run(["git", "-C", str(repo), "branch", BRANCH], check=True, capture_output=True)
    store.states = ["approved"]
    calls = _fetching(monkeypatch, repo, brings=False)
    rc, _ran = _land(repo, tmp_path, monkeypatch, store, extra=("--phase", "seams"), phase=True)
    assert (rc, calls) == (0, [])


def test_an_all_done_phase_with_its_branch_gone_takes_the_already_landed_path(repo, tmp_path, monkeypatch, store, capsys):  # noqa: F811
    _remote_run(tmp_path)
    monkeypatch.setattr(cli, "_phase_merged_pr", lambda *args: {"number": 535, "state": "merged"})
    store.states = ["done"]
    calls = _fetching(monkeypatch, repo, brings=False)
    rc, ran = _land(repo, tmp_path, monkeypatch, store, extra=("--phase", "seams"), phase=True)
    out = capsys.readouterr().out
    assert (rc, ran, calls) == (0, ["clean_phase", "mark_done"], [])
    assert "fetch_phase_branch" not in out


def test_a_run_with_no_remote_record_skips_the_check(repo, tmp_path, monkeypatch, store, capsys):  # noqa: F811
    store.states = ["approved"]
    calls = _fetching(monkeypatch, repo, brings=False)
    rc, _ran = _land(repo, tmp_path, monkeypatch, store, extra=("--phase", "seams"), phase=True)
    assert (rc, calls) == (0, [])
    assert "fetch_phase_branch" not in capsys.readouterr().out
    assert not remote_lane.remote_record_path(tmp_path / "runs", "epic-x-5").exists()
