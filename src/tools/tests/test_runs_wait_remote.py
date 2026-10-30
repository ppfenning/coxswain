from __future__ import annotations

import argparse

from agent_tools import cli as cli_module
from agent_tools import epic, runs_top, runs_top_screen


def _remote(run: str) -> runs_top.Row:
    return runs_top.remote_row(run, "boat-2", 3)


def _local(run: str, alive: bool = True) -> runs_top.Row:
    return runs_top.Row(run=run, alive=alive, phase="", node="n", attempt=1, turns=1, cost_usd=0.0, verdict="", status="running")


def _args(tmp_path, **over) -> argparse.Namespace:
    return argparse.Namespace(**{"runs_dir": str(tmp_path), "max_seconds": 3600.0, "interval": 15.0, "json": False, **over})


def _script(*polls: list[runs_top.Row]):
    """A `rows_fn` that answers each call with the next scripted poll and repeats the last one."""
    calls = []

    def rows_fn(_runs_dir):
        calls.append(1)
        return list(polls[min(len(calls), len(polls)) - 1])

    return rows_fn


def _clock():
    now = [0.0]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    return (lambda: now[0]), sleep, slept


def test_waitable_keeps_live_rows_local_and_remote_and_drops_a_just_exited_one():
    rows = [_local("b"), _local("old", alive=False), _remote("a")]
    assert runs_top_screen.waitable(rows) == ["a", "b"]


def test_remote_run_ids_names_only_the_remote_rows():
    assert runs_top_screen.remote_run_ids([_local("b"), _remote("a")]) == {"a"}


def test_wait_with_only_remote_lanes_blocks_then_returns_on_the_first_remote_exit(tmp_path, capsys):
    (tmp_path / "r1.log").write_text("approved but not landed: t7\nepic r1: done\n")
    (tmp_path / "r1.fetched.json").write_text("{}")
    both, one = [_remote("r1"), _remote("r2")], [_remote("r2")]
    clock, sleep, slept = _clock()
    rc = cli_module._runs_wait(_args(tmp_path, interval=5.0), rows_fn=_script(both, both, both, one), clock=clock, sleep=sleep)
    out = capsys.readouterr().out
    assert rc == 0
    assert slept == [5.0, 5.0]
    assert "exited r1 at " in out and "approved but not landed: t7" in out and "exited r2" not in out


def test_a_remote_run_not_yet_local_is_fetched_before_its_outcome_is_printed(tmp_path, capsys):
    order = []

    def fetch(runs_dir, run):
        order.append(("fetch", run))
        (tmp_path / f"{run}.log").write_text("epic r1: done\n")
        (tmp_path / f"{run}.fetched.json").write_text("{}")
        return "fetched", [], "boat-2"

    clock, sleep, _ = _clock()
    rows_fn = _script([_remote("r1")], [_remote("r1")], [])
    assert cli_module._runs_wait(_args(tmp_path), rows_fn=rows_fn, clock=clock, sleep=sleep, fetch=fetch) == 0
    assert order == [("fetch", "r1")]
    assert "epic r1: done" in capsys.readouterr().out


def test_a_remote_run_already_fetched_is_not_fetched_again(tmp_path, capsys):
    (tmp_path / "r1.fetched.json").write_text("{}")
    clock, sleep, _ = _clock()

    def fetch(runs_dir, run):
        raise AssertionError("fetched twice")

    cli_module._runs_wait(_args(tmp_path), rows_fn=_script([_remote("r1")], []), clock=clock, sleep=sleep, fetch=fetch)
    assert "exited r1 at " in capsys.readouterr().out


def test_a_failed_fetch_is_printed_and_the_run_is_still_reported_exited(tmp_path, capsys):
    clock, sleep, _ = _clock()
    fetch = lambda runs_dir, run: ("failed", ["fetch: host boat-2 is unreachable"], "")  # noqa: E731
    rc = cli_module._runs_wait(_args(tmp_path), rows_fn=_script([_remote("r1")], []), clock=clock, sleep=sleep, fetch=fetch)
    out = capsys.readouterr().out
    assert rc == 0 and "r1: fetch: host boat-2 is unreachable" in out and "exited r1 at " in out


def test_wait_with_no_live_lanes_prints_all_lanes_clear(tmp_path, capsys):
    rc = cli_module._runs_wait(_args(tmp_path), rows_fn=_script([_local("old", alive=False)]))
    assert rc == 2 and capsys.readouterr().out.strip() == "all lanes clear"


def test_wait_on_a_local_lane_never_fetches(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.log").write_text("epic a: done\n")
    clock, sleep, _ = _clock()
    monkeypatch.setattr(epic, "run_live", lambda *args, **kw: False)

    def fetch(runs_dir, run):
        raise AssertionError("local run fetched")

    rc = cli_module._runs_wait(_args(tmp_path), rows_fn=_script([_local("a")], []), clock=clock, sleep=sleep, fetch=fetch)
    assert rc == 0 and "exited a at " in capsys.readouterr().out
