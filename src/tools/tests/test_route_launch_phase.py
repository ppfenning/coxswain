import argparse
import json

import pytest
from test_cli_launch_on import (  # noqa: F401  (the two underscore fixtures are autouse)
    _fake_edge,
    _logged_in_host,
    _no_ccusage,
    _no_local_process,
    _run_files,
    _setup,
)

from agent_tools import cli, epic, route
from agent_tools.cli import main
from agent_tools.remote_argv import launch_argv

pytestmark = pytest.mark.usefixtures("board_store")  # the store is the board; these launches read its rows


def _two_phases(ws):
    (ws / "work" / "demo" / "p2").mkdir()
    (ws / "work" / "demo" / "p2" / "u.md").write_text("---\nid: u\nstate: ready\n---\n\nBody\n")


def _live(monkeypatch, ws, run):
    """A pidfile for `run` that reads as a live process."""
    (ws / "runs" / f"{run}.pid").write_text("1")
    monkeypatch.setattr(epic, "run_state", lambda pid, pidfile, now=None: "running")


def _dry_local(argv, *extra):
    return main([*argv, "--local", "--no-claim", "--dry-run", *extra])


def test_phase_dry_run_argv_carries_the_phase_and_the_run_id_starts_with_the_phase_prefix(tmp_path, capsys):
    _, _, argv = _setup(tmp_path)
    rc = _dry_local(argv, "--phase", "p1")
    line = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("dry-run: "))
    words = line.split()
    assert rc == 0
    assert words[words.index("--phase") + 1] == "p1"
    assert words[words.index("--run-id") + 1] == "demo.p1-1"


def test_launch_without_phase_is_unchanged(tmp_path, capsys):
    _, _, argv = _setup(tmp_path)
    rc = _dry_local(argv)
    words = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("dry-run: ")).split()
    assert rc == 0 and "--phase" not in words and words[words.index("--run-id") + 1] == "demo-1"


def test_two_phases_start_two_runs_with_distinct_ids_in_the_order_given(tmp_path, monkeypatch, capsys):
    ws, _, argv = _setup(tmp_path)
    _two_phases(ws)
    _fake_edge(monkeypatch, [])
    _no_local_process(monkeypatch)
    rc = main([*argv, "--on", "box", "--label", "lbl", "--phase", "p2", "--phase", "p1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert [l for l in out.splitlines() if l.startswith("run ")] == ["run demo.p2-1", "run demo.p1-1"]
    assert _run_files(ws) == ["demo.p1-1.remote.json", "demo.p2-1.remote.json"]


def test_a_failing_first_phase_stops_the_loop_and_returns_its_code(tmp_path, monkeypatch, capsys):
    ws, _, argv = _setup(tmp_path)
    _two_phases(ws)
    _fake_edge(monkeypatch, [], codes={"ssh": 1})
    rc = main([*argv, "--on", "box", "--label", "lbl", "--phase", "p1", "--phase", "p2"])
    assert rc == 2
    assert _run_files(ws) == []


def test_a_live_run_of_another_phase_does_not_refuse_and_one_of_the_same_phase_does(tmp_path, monkeypatch, capsys):
    ws, _, argv = _setup(tmp_path)
    _two_phases(ws)
    _live(monkeypatch, ws, "demo.p2-1")
    assert _dry_local(argv, "--phase", "p1") == 0
    capsys.readouterr()
    _live(monkeypatch, ws, "demo.p1-1")
    assert _dry_local(argv, "--phase", "p1") == 2
    assert "demo.p1-1 is already running" in capsys.readouterr().out


def test_an_unknown_phase_is_refused_before_anything_is_written(tmp_path, capsys):
    ws, _, argv = _setup(tmp_path)
    rc = _dry_local(argv, "--phase", "nope")
    assert rc == 2
    assert "has no phase nope; phases: p1" in capsys.readouterr().out
    assert _run_files(ws) == []


def test_phase_on_another_graph_or_with_run_id_and_many_phases_is_refused(tmp_path, capsys):
    _, _, argv = _setup(tmp_path)
    assert cli._route_launch(argparse.Namespace(graph="rescue", phase=["p1"])) == 2
    assert "epic graph only" in capsys.readouterr().out
    assert _dry_local(argv, "--phase", "p1", "--phase", "p2", "--run-id", "x-1") == 2
    assert "--run-id names one run" in capsys.readouterr().out


def test_on_dry_run_with_a_phase_puts_it_in_the_remote_argv(tmp_path, monkeypatch, capsys):
    _, _, argv = _setup(tmp_path)
    _fake_edge(monkeypatch, [])
    rc = main([*argv, "--on", "box", "--no-claim", "--dry-run", "--phase", "p1"])
    ssh = [l for l in capsys.readouterr().out.splitlines() if l.startswith("dry-run: ssh")][-1]
    assert rc == 0
    assert "--phase p1" in ssh and "--run-id demo.p1-1" in ssh


def test_an_on_launch_with_a_phase_records_the_run_and_passes_the_phase(tmp_path, monkeypatch, capsys):
    ws, _, argv = _setup(tmp_path)
    calls = []
    _fake_edge(monkeypatch, calls)
    _no_local_process(monkeypatch)
    rc = main([*argv, "--on", "box", "--label", "lbl", "--phase", "p1"])
    assert rc == 0
    assert "--phase p1" in calls[-1][-1]
    assert json.loads((ws / "runs" / "demo.p1-1.remote.json").read_text())["initiative"] == "demo"


@pytest.mark.parametrize(
    ("name", "run_id"),
    [
        ("demo.p1-1.remote.json", "demo.p1-1"),
        ("demo.p1-1.launched.json", "demo.p1-1"),
        ("demo.p1-1.log", "demo.p1-1"),
        ("demo.p1-1.pid", "demo.p1-1"),
        ("demo.p1-1-trace", "demo.p1-1"),
        ("demo.p1-1:t1.json", "demo.p1-1"),
        ("demo-1.log", "demo-1"),
        ("demo-1.remote.json", "demo-1"),
    ],
)
def test_run_id_of_cuts_only_known_suffixes(name, run_id):
    assert cli._run_id_of(name) == run_id


def test_a_remote_phase_runs_files_reserve_its_dotted_id(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "demo.p1-1.remote.json").write_text("{}")
    assert route.next_run_id(cli._taken_run_names(runs), route.phase_run_prefix("demo", "p1")) == "demo.p1-2"
    assert cli.remote_lane.refuse_taken_run_id("demo.p1-1", set(cli._taken_run_names(runs))) is not None


def test_launch_argv_phase_is_still_the_last_pair():
    assert launch_argv("i", "r", "l", "p1")[-2:] == ["--phase", "p1"]
