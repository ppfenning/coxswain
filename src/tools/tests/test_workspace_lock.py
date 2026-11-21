"""`workspace_lock`: one bounded flock for the workspace's writers (pull, fill, export, commit), which run in more than
one process once the queue workers do the tick's chores. Two real processes on a real checkout: no lost write and no
git index lock error."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from agent_tools import workspace_lock


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "chair@example.com")
    _git(path, "config", "user.name", "chair")
    return path


def test_the_lock_lives_in_the_git_dir_or_the_workspace_out_of_git(tmp_path: Path):
    repo = _repo(tmp_path / "ws")
    assert workspace_lock.lock_path(repo) == repo / ".git" / workspace_lock.LOCK_NAME
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {repo / '.git' / 'worktrees' / 'wt'}\n")
    assert workspace_lock.git_dir(worktree) == repo / ".git" / "worktrees" / "wt"
    assert workspace_lock.lock_path(tmp_path / "plain") == tmp_path / "plain" / workspace_lock.LOCK_NAME


def test_a_nested_writer_in_the_same_process_does_not_wait_on_its_callers_hold(tmp_path: Path):
    repo = _repo(tmp_path / "ws")
    ran = []
    with workspace_lock.held(repo, "export", bound=0.2), workspace_lock.held(repo, "fill", bound=0.2):
        ran.append("fill")
    assert ran == ["fill"]


def _hold_in_child(repo: Path) -> subprocess.Popen:
    code = (
        "import sys, time; from pathlib import Path; from agent_tools import workspace_lock\n"
        "with workspace_lock.held(Path(sys.argv[1]), 'commit'):\n"
        "    print('held', flush=True); time.sleep(30)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", code, str(repo)], stdout=subprocess.PIPE, text=True)
    assert child.stdout is not None and child.stdout.readline().strip() == "held"
    return child


def test_a_writer_past_the_bound_is_skipped_with_one_line_naming_the_holder(tmp_path: Path):
    repo = _repo(tmp_path / "ws")
    child = _hold_in_child(repo)
    try:
        lines: list[str] = []
        ran = []
        with pytest.raises(workspace_lock.WorkspaceBusy), workspace_lock.held(repo, "pull", bound=0.3, log=lines.append):
            ran.append("pull")
        assert ran == []
        assert lines == [f"workspace busy: skipped the pull; the workspace lock was held by pid {child.pid} (commit) past 0.3s"]
        assert workspace_lock.skipped(lines[0])
    finally:
        child.kill()
        child.wait()
    with workspace_lock.held(repo, "pull", bound=5):  # the holder's exit released it
        pass


def test_the_pull_reports_a_skip_as_its_reason_and_runs_no_git(tmp_path: Path, monkeypatch):
    from agent_tools import workspace_sync

    repo = _repo(tmp_path / "ws")
    monkeypatch.setattr(workspace_lock, "BOUND_SECONDS", 0.2)
    child = _hold_in_child(repo)
    try:
        monkeypatch.setattr(workspace_sync, "_git", lambda *a: pytest.fail("git ran while the lock was held"))
        assert workspace_sync.pull_workspace(repo).startswith("workspace busy: skipped the pull")
    finally:
        child.kill()
        child.wait()


def test_another_thread_of_the_process_waits_for_the_hold(tmp_path: Path):
    repo = _repo(tmp_path / "ws")
    order: list[str] = []
    entered = threading.Event()

    def other() -> None:
        entered.wait()
        with workspace_lock.held(repo, "fill", bound=5):
            order.append("other")

    thread = threading.Thread(target=other)
    thread.start()
    with workspace_lock.held(repo, "export", bound=5):
        entered.set()
        thread.join(0.3)
        order.append("first")
    thread.join(5)
    assert order == ["first", "other"]


_COMMITTER = """
import sys
from pathlib import Path
from agent_tools import cli_board

ws, tag, n = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
count = [0]

def export(runs_dir, workspace, prune_stale=False):  # the store's rows, as files: one new file per export
    (workspace / "work" / f"{tag}-{count[0]}.md").write_text(tag + "\\n")
    count[0] += 1
    return []

cli_board._export_board = export
for _ in range(n):
    cli_board._chair_export_hook(ws / "runs", (tag,))  # the real hook: git add, diff and commit, under the lock
"""


def test_two_processes_exporting_and_committing_at_once_lose_no_file_and_never_meet_the_index_lock(tmp_path: Path):
    ws = _repo(tmp_path / "ws")
    (ws / "work").mkdir()
    (ws / "runs").mkdir()
    (ws / "work" / "seed.md").write_text("seed\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "seed")
    n = 12
    writers = [subprocess.Popen([sys.executable, "-c", _COMMITTER, str(ws), tag, str(n)], stderr=subprocess.PIPE, text=True)
               for tag in ("housekeeping", "launch")]
    errors = [w.communicate(timeout=120)[1] for w in writers]
    assert [w.returncode for w in writers] == [0, 0], errors
    assert not any("index.lock" in e for e in errors)
    tracked = set(_git(ws, "ls-files", "work").split())
    assert tracked == {"work/seed.md", *(f"work/{tag}-{i}.md" for tag in ("housekeeping", "launch") for i in range(n))}
    assert _git(ws, "status", "--porcelain").strip() == ""


_EXPORTER = """
import json, sys, time
from pathlib import Path
from types import SimpleNamespace
from agent_tools import chair_decompose_runs, cli_common, queue_export, run_store, store_fill

ws, store, n = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])

def fill(workspace, prune_stale=False):  # the store learns every intake file there is
    names = sorted({*json.loads(store.read_text()), *(p.name for p in (workspace / "intake").glob("*.md"))})
    store.write_text(json.dumps(names))
    time.sleep(0.05)  # a fill takes time; a pull that lands now brings a file this fill never read
    return ""

def export(workspace, rows, **_):  # as the real export: a file with no row is deleted
    for path in (workspace / "intake").glob("*.md"):
        if path.name not in rows:
            path.unlink()
    return SimpleNamespace(skipped=[])

store_fill.fill_workspace = fill
run_store.read_queue = lambda runs_dir: json.loads(store.read_text())
queue_export.export_files = export
queue_export.store_hashes = lambda runs_dir: None
chair_decompose_runs.live_decompose_initiatives = lambda runs_dir: frozenset()
for _ in range(n):
    cli_common._export_board(ws / "runs", ws)  # the real board export: one hold from the fill to the last write
"""

_PULLER = """
import subprocess, sys
from pathlib import Path
from agent_tools import workspace_sync

ws, other, n = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
for i in range(n):  # another machine files an intake and pushes it; the chair's workspace pulls it in
    (other / "intake" / f"filed-{i}.md").write_text("an intake\\n")
    for args in (["add", "-A"], ["commit", "-q", "-m", f"filed {i}"], ["push", "-q", "origin", "main"]):
        subprocess.run(["git", *args], cwd=other, check=True, capture_output=True)
    reason = workspace_sync.pull_workspace(ws)  # the real pull, --ff-only --autostash, under the lock
    if reason:
        sys.exit(reason)
"""


def test_a_pull_and_a_board_export_at_once_never_lose_a_pulled_file(tmp_path: Path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    seed = _repo(tmp_path / "seed")
    (seed / "intake").mkdir()
    (seed / "intake" / "first.md").write_text("first\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "seed")
    _git(seed, "push", "-q", str(remote), "main")
    for name in ("ws", "other"):
        subprocess.run(["git", "clone", "-q", str(remote), str(tmp_path / name)], check=True)
        _git(tmp_path / name, "config", "user.email", "chair@example.com")
        _git(tmp_path / name, "config", "user.name", "chair")
    ws, other = tmp_path / "ws", tmp_path / "other"
    (ws / "runs").mkdir()
    store = tmp_path / "store.json"
    store.write_text("[]")
    n = 10
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    writers = [
        subprocess.Popen([sys.executable, "-c", _EXPORTER, str(ws), str(store), str(n * 4)], stderr=subprocess.PIPE, text=True, env=env),
        subprocess.Popen([sys.executable, "-c", _PULLER, str(ws), str(other), str(n)], stderr=subprocess.PIPE, text=True, env=env),
    ]
    errors = [w.communicate(timeout=180)[1] for w in writers]
    assert [w.returncode for w in writers] == [0, 0], errors
    assert not any("index.lock" in e for e in errors)
    on_disk = {p.name for p in (ws / "intake").glob("*.md")}
    assert on_disk == {"first.md", *(f"filed-{i}.md" for i in range(n))}
    assert _git(ws, "status", "--porcelain").strip() == ""
