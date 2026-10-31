import argparse
import os
import socket
import subprocess as sp

from test_run_store import leases_table

from agent_tools import cleanup, cli

FUTURE = "2999-01-01T00:00:00Z"
DEAD_PID = "999999999"


def _runs(tmp_path, run, pid, holder):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / f"{run}.pid").write_text(str(pid))
    if holder is not None:
        leases_table(runs, (f"runs:{run.rsplit('-', 1)[0]}", holder, FUTURE))
    return runs


def test_a_live_process_with_a_foreign_lease_holder_gets_the_fenced_refusal(tmp_path):
    runs = _runs(tmp_path, "cos-1", os.getpid(), "cos-2")
    msg = cli._refuse_if_already_running(runs, "cos")
    assert str(os.getpid()) in msg and socket.gethostname() in msg
    assert "cos-1" in msg and "epoch fence" in msg and "Stop pid" in msg
    assert "is already running" not in msg


def test_a_live_process_holding_its_lease_gets_the_running_refusal_with_the_host(tmp_path):
    runs = _runs(tmp_path, "cos-1", os.getpid(), "cos-1")
    msg = cli._refuse_if_already_running(runs, "cos")
    assert msg == f"routing: cos-1 is already running (pid {os.getpid()}) on {socket.gethostname()}"


def test_a_dead_pidfile_is_not_a_refusal(tmp_path):
    assert cli._refuse_if_already_running(_runs(tmp_path, "cos-1", DEAD_PID, None), "cos") is None


def test_a_dead_pidfile_holding_the_lease_is_not_a_refusal(tmp_path):
    assert cli._refuse_if_already_running(_runs(tmp_path, "cos-1", DEAD_PID, "cos-1"), "cos") is None


def test_clean_on_a_fenced_run_prints_the_skipped_line_and_removes_nothing(tmp_path, capsys):
    root = tmp_path / "r"
    root.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    sp.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    (root / "f").write_text("x")
    sp.run(["git", "-C", str(root), "add", "-A"], check=True, env=env)
    sp.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True, env=env)
    sp.run(["git", "-C", str(root), "branch", "agents/epic-x-5/task"], check=True)
    wt = tmp_path / "wt" / "epic-x-5" / "seams"
    wt.parent.mkdir(parents=True)
    sp.run(["git", "-C", str(root), "worktree", "add", "-q", str(wt), "agents/epic-x-5/task"], check=True)
    runs = _runs(tmp_path, "epic-x-5", os.getpid(), "epic-x-6")
    before = cleanup.git_worktrees(root)
    a = argparse.Namespace(repo=str(root), run_id="epic-x-5", runs_dir=str(runs), worktree_root=str(tmp_path / "wt"), apply=True, force=True)
    assert cli._runs_clean(a) == 0
    out = capsys.readouterr().out
    assert f"skipped run epic-x-5: pid {os.getpid()} is fenced" in out
    assert "removed" not in out
    assert cleanup.git_worktrees(root) == before and wt.exists()
    assert "agents/epic-x-5/task" in cleanup.git_branches(root)
