"""`lane_rollup` against real git, one test per assumption the lane makes of it: the trailers a roll-up commits are the
ones `git log` reads back as progress; a push that is not a fast-forward is refused; a change that does not apply
cleanly commits nothing and leaves the phase worktree as it was."""

import subprocess
from pathlib import Path

from harness import lane_progress, lane_rollup


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    """A bare origin with `main` holding one file, and a clone of it; the base commit's sha."""
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True, capture_output=True)
    (repo / "a.txt").write_text("one\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")
    _git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    return repo, _git(repo, "rev-parse", "HEAD")


def _diff(repo: Path, base: str, path: str, text: str) -> str:
    """The binary diff of writing `text` to `path` on top of `base`, made in a worktree of its own and thrown away."""
    scratch = repo.parent / f"scratch-{len(list(repo.parent.glob('scratch-*')))}"
    _git(repo, "worktree", "add", "-q", "--detach", str(scratch), base)
    (scratch / path).write_text(text)
    _git(scratch, "add", "-A")
    _git(scratch, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x")
    ok, diff = lane_rollup.change(scratch, base)
    assert ok, diff
    return diff


def test_rolled_up_commits_are_read_back_as_the_phases_progress(tmp_path):
    repo, base = _repo(tmp_path)
    phase = tmp_path / "phase"
    assert lane_rollup.open_phase_worktree(repo, phase, base)[0]
    for task in ("t1", "t2"):
        sha, why = lane_rollup.apply_and_commit(phase, _diff(repo, base, f"{task}.txt", task),
                                                lane_rollup.message("ship", "p1", task, "ship.p1-1", f"Task {task}"))
        assert sha is not None, why
        assert lane_rollup.push(phase, "epic/ship/p1")[0]
    tip, why = lane_rollup.fetch(repo, "ship.p1-2", "epic/ship/p1")
    assert tip is not None and not why
    ok, log = lane_rollup.log_since(repo, base, tip)
    assert ok, log
    progress = lane_progress.read(log, "ship", "p1")
    assert list(progress.done) == ["t1", "t2"] and progress.done["t2"] == tip and progress.ignored == ()
    assert lane_rollup.fetch(repo, "ship.p1-2", "epic/ship/p9") == (None, "")


def test_a_push_that_is_not_a_fast_forward_is_refused(tmp_path):
    repo, base = _repo(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    for path in (first, second):
        assert lane_rollup.open_phase_worktree(repo, path, base)[0]
    for path, task in ((first, "t1"), (second, "t2")):
        sha, why = lane_rollup.apply_and_commit(path, _diff(repo, base, f"{task}.txt", task),
                                                lane_rollup.message("ship", "p1", task, "r", task))
        assert sha is not None, why
    assert lane_rollup.push(first, "epic/ship/p1")[0]
    pushed, detail = lane_rollup.push(second, "epic/ship/p1")
    assert not pushed and "rejected" in detail


def test_a_change_that_does_not_apply_commits_nothing_and_leaves_the_worktree_clean(tmp_path):
    repo, base = _repo(tmp_path)
    phase = tmp_path / "phase"
    lane_rollup.open_phase_worktree(repo, phase, base)
    assert lane_rollup.apply_and_commit(phase, _diff(repo, base, "a.txt", "two\n"), "m")[0] is not None
    sha, why = lane_rollup.apply_and_commit(phase, _diff(repo, base, "a.txt", "three\n"), "m")
    assert sha is None and "does not apply cleanly" in why
    assert _git(phase, "status", "--porcelain") == ""
    assert (phase / "a.txt").read_text() == "two\n"
