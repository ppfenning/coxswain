import os
from dataclasses import replace
from pathlib import Path

from test_run_store import leases_table

from agent_tools import chair_exec
from agent_tools.chair_exec import Deps, perform
from tests.test_chair_exec import _clear, _deps

DEAD_PID = 999999999
SSH = "ops@host-a"


def _recording(calls: list[list[str]], runs_dir: Path, listing: str = "") -> Deps:
    """A Deps whose `run` records each argv and whose runs dir is `runs_dir`."""

    def run(argv):
        calls.append(argv)
        return 0, listing

    swept: list = []
    base = _deps(swept)
    return replace(
        base,
        run=run,
        runs_dir=runs_dir,
        delete_branches=lambda repo, pattern: calls.append(["delete", repo, pattern]) or ([], ""),
        ssh_for=lambda host: SSH if host == "host-a" else "",
    )


def _pidfile(runs_dir: Path, name: str, pid: int) -> None:
    (runs_dir / f"{name}.pid").write_text(str(pid))


def test_a_live_run_blocks_the_clear_before_any_git_call(tmp_path) -> None:
    _pidfile(tmp_path, "B-1", os.getpid())
    calls: list[list[str]] = []
    [result] = perform([{**_clear("B"), "repo": "R"}], _recording(calls, tmp_path), lambda: 1, False)
    assert result["status"] == "failed"
    assert result["reason"] == f"clear refused, a run of this initiative is live: B-1 pid {os.getpid()} running"
    assert calls == []


def test_a_fenced_run_blocks_the_clear_the_same_way(tmp_path) -> None:
    leases_table(tmp_path, ("runs:B", "B-9", "2999-01-01T00:00:00Z"))
    _pidfile(tmp_path, "B-1", os.getpid())
    calls: list[list[str]] = []
    [result] = perform([{**_clear("B"), "repo": "R"}], _recording(calls, tmp_path), lambda: 1, False)
    assert result["status"] == "failed"
    assert result["reason"] == f"clear refused, a run of this initiative is live: B-1 pid {os.getpid()} fenced"
    assert calls == []


def test_each_live_run_is_named_in_the_line() -> None:
    runs = [{"run": "B-1", "pid": 11, "state": "running"}, {"run": "B-2", "pid": 22, "state": "fenced"}]
    assert chair_exec._live_runs_line(runs) == "clear refused, a run of this initiative is live: B-1 pid 11 running; B-2 pid 22 fenced"
    assert chair_exec._live_runs_line([]) == ""


def test_a_dead_pidfile_leaves_the_existing_prune_untouched(tmp_path) -> None:
    _pidfile(tmp_path, "B-1", DEAD_PID)
    listing = "worktree R\nHEAD abc\nbranch refs/heads/main\n\nworktree /w/B\nHEAD abc\nbranch refs/heads/epic/B/p1\n\n"
    calls: list[list[str]] = []
    [result] = perform([{**_clear("B"), "repo": "R"}], _recording(calls, tmp_path, listing), lambda: 1, False)
    assert result["status"] == "done"
    assert calls[0] == ["git", "-C", "R", "worktree", "list", "--porcelain"]
    assert ["git", "-C", "R", "worktree", "remove", "--force", "/w/B"] in calls
    assert ["git", "-C", "R", "branch", "-D", "epic/B/p1"] in calls


def test_another_initiative_s_live_run_does_not_block_the_clear(tmp_path) -> None:
    _pidfile(tmp_path, "A-1", os.getpid())
    calls: list[list[str]] = []
    [result] = perform([{**_clear("B"), "repo": "R"}], _recording(calls, tmp_path), lambda: 1, False)
    assert result["status"] == "done"
    assert calls[0] == ["git", "-C", "R", "worktree", "list", "--porcelain"]


def test_the_host_prune_refuses_on_a_live_local_pidfile_without_an_ssh_call(tmp_path) -> None:
    _pidfile(tmp_path, "B-1", os.getpid())
    calls: list[list[str]] = []
    deps = _recording(calls, tmp_path)
    deleted, error = chair_exec._pruned(chair_exec._over_ssh(deps, SSH), "R", "B", "epic/B/*", set(), "~/R")
    assert (deleted, error) == ([], f"clear refused, a run of this initiative is live: B-1 pid {os.getpid()} running")
    assert calls == []


def test_a_host_clear_with_a_live_local_run_runs_nothing_anywhere(tmp_path) -> None:
    _pidfile(tmp_path, "B-1", os.getpid())
    calls: list[list[str]] = []
    [result] = perform([{**_clear("B"), "repo": "R", "host": "host-a"}], _recording(calls, tmp_path), lambda: 1, False)
    assert result["status"] == "failed"
    assert calls == []
